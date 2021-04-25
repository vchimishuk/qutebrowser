# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2015-2020 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Simple history which gets written to disk."""

import os
import time
import contextlib
import typing

from PyQt5.QtCore import pyqtSlot, QUrl, pyqtSignal
from PyQt5.QtWidgets import QProgressDialog, QApplication

from qutebrowser.config import config
from qutebrowser.api import cmdutils
from qutebrowser.utils import (utils, log, usertypes, message, qtutils,
                               standarddir, objreg)
from qutebrowser.misc import objects, sql


# increment to indicate that HistoryCompletion must be regenerated
_USER_VERSION = 3
web_history = typing.cast('WebHistory', None)


class HistoryProgress:

    """Progress dialog for history imports/conversions.

    This makes WebHistory simpler as it can call methods of this class even
    when we don't want to show a progress dialog (for very small imports). This
    means tick() and finish() can be called even when start() wasn't.
    """

    def __init__(self):
        self._progress = None
        self._value = 0

    def start(self, text, maximum):
        """Start showing a progress dialog."""
        self._progress = QProgressDialog()
        self._progress.setMinimumDuration(500)
        self._progress.setLabelText(text)
        self._progress.setMaximum(maximum)
        self._progress.setCancelButton(None)
        self._progress.show()
        QApplication.processEvents()

    def tick(self):
        """Increase the displayed progress value."""
        self._value += 1
        if self._progress is not None:
            self._progress.setValue(self._value)
            QApplication.processEvents()

    def finish(self):
        """Finish showing the progress dialog."""
        if self._progress is not None:
            self._progress.hide()


class CompletionMetaInfo(sql.SqlTable):

    """Table containing meta-information for the completion."""

    KEYS = {
        'force_rebuild': False,
    }

    def __init__(self, parent=None):
        super().__init__("CompletionMetaInfo", ['key', 'value'],
                         constraints={'key': 'PRIMARY KEY'})
        for key, default in self.KEYS.items():
            if key not in self:
                self[key] = default

    def _check_key(self, key):
        if key not in self.KEYS:
            raise KeyError(key)

    def __contains__(self, key):
        self._check_key(key)
        query = self.contains_query('key')
        return query.run(val=key).value()

    def __getitem__(self, key):
        self._check_key(key)
        query = sql.Query('SELECT value FROM CompletionMetaInfo '
                          'WHERE key = :key')
        return query.run(key=key).value()

    def __setitem__(self, key, value):
        self._check_key(key)
        self.insert({'key': key, 'value': value}, replace=True)


class CompletionHistory(sql.SqlTable):

    """History which only has the newest entry for each URL.

    Attributes:
        _progress: A HistoryProgress instance.

    Class attributes:
        _PROGRESS_THRESHOLD: When to start showing progress dialogs.
        _NAME: Table name.
    """

    _PROGRESS_THRESHOLD = 1000
    _NAME = 'CompletionHistory'

    def __init__(self, parent=None, db=None):
        super().__init__(self._NAME, ['id', 'url', 'title', 'first_atime',
                                      'last_atime', 'visits', 'frecency'],
                         constraints={'id': 'INTEGER PRIMARY KEY',
                                      'url': 'TEXT NOT NULL',
                                      'title': 'TEXT NOT NULL',
                                      'visits': 'INTEGER NOT NULL',
                                      'first_atime': 'INTEGER NOT NULL',
                                      'last_atime': 'INTEGER NOT NULL',
                                      'frecency': 'INTEGER NOT NULL'},
                         parent=parent, db=db)
        self.create_index(self._NAME + 'UrlIndex', 'url', unique=True)
        self.create_index(self._NAME + 'FrecencyIndex', 'frecency')
        self._progress = HistoryProgress()

    def init(self, items):
        if len(items) > self._PROGRESS_THRESHOLD:
            self._progress.start("Rebuilding completion...", len(items))

        data = {
            'url': [],
            'title': [],
            'visits': [],
            'first_atime': [],
            'last_atime': [],
            'frecency': []
        }  # type: typing.Mapping[str, typing.MutableSequence[str]]
        for item in items:
            self._progress.tick()
            url = QUrl(item.url)
            if self._is_excluded(url):
                continue

            data['url'].append(self._format_completion_url(url))
            data['title'].append(item.title)
            data['visits'].append(item.visits)
            data['first_atime'].append(item.first_atime)
            data['last_atime'].append(item.last_atime)
            # Frecency will be calculated later, with periodic recalculation.
            data['frecency'].append(0)

        self._progress.finish()
        self.insert_batch(data, replace=True)

    def add(self, url):
        if self._is_excluded(url):
            return

        u = {'url': self._format_completion_url(url['url']),
             'title': url['title'],
             'visits': 1,
             'first_atime': url['atime'],
             'last_atime': url['atime'],
             'frecency': 1}
        update = {'visits': 'visits + 1',
                  'frecency': 'frecency + 1',
                  'last_atime': u['last_atime']}

        self.upsert(u, 'url', update, escape=False)

    def delete(self, url):
        self.delete('url', self._format_completion_url(url))

    def update_frecency(self):
        def update(executor):
            day_secs = 60 * 60 * 24
            today = int(time.time())
            s = ('UPDATE ' + self._NAME + ' SET '
                 'frecency = visits '
                 '* (MAX(last_atime - first_atime, :day_secs) / :day_secs) '
                 '/ ((MAX(:today - last_atime, :day_secs) / :day_secs) '
                 '* (MAX(:today - last_atime, :day_secs) / :day_secs)) '
                 'WHERE id >= :start AND id < :end')
            db = sql.open_db(history_db_path(), 'completion')
            size = 5000
            i = size

            try:
                # SQLite locks DB during write, so other connections cannot
                # write at that time. To prevent very long lock hold we break
                # whole table update in smaller chunks, so other connection
                # gets a chance to aquire the lock.
                while not executor.is_shutting_down():
                    q = sql.Query(s, db=db)
                    q.run(today=today, day_secs=day_secs, start=i - size, end=i)
                    if not q.rows_affected():
                        break
                    i += size
                    # In my tests I can see that other thread cannot aquire
                    # (most of the time) DB lock without this sleep. Anyway,
                    # We do not care about speed to much here.
                    time.sleep(0.02)
            except Exception as e:
                log.misc.error('Update history frecency: {}'.format(e))
            finally:
                name = db.connectionName()
                del db
                sql.close_db(db)

        log.misc.info('Updating history frecency scores...')
        objreg.get('task-executor').submit(update)

    def _format_completion_url(self, url):
        return url.toString(QUrl.RemovePassword)

    def _is_excluded(self, url):
        """Check if the given URL is excluded from the completion."""
        patterns = config.cache['completion.web_history.exclude']

        return any(pattern.matches(url) for pattern in patterns)

    @staticmethod
    def drop():
        q = sql.Query("DROP TABLE IF EXISTS {}".format(CompletionHistory._NAME))
        q.run()


class WebHistory(sql.SqlTable):

    """The global history of visited pages.

    Attributes:
        completion: A CompletionHistory instance.
        _metainfo: A CompletionMetaInfo instance.
    """

    # All web history cleared
    history_cleared = pyqtSignal()
    # one url cleared
    url_cleared = pyqtSignal(QUrl)

    def __init__(self, parent=None):
        super().__init__("History", ['url', 'title', 'atime', 'redirect'],
                         constraints={'url': 'NOT NULL',
                                      'title': 'NOT NULL',
                                      'atime': 'NOT NULL',
                                      'redirect': 'NOT NULL'},
                         parent=parent)
        self._metainfo = CompletionMetaInfo(parent=self)
        # Store the last saved url to avoid duplicate immediate saves.
        self._last_url = None

        old_ver = self._get_version() < _USER_VERSION
        rebuild = self._metainfo['force_rebuild']

        if old_ver or rebuild:
            CompletionHistory.drop()
            self._metainfo['force_rebuild'] = False
        self.completion = CompletionHistory(self)
        if not self.completion:
            self.completion.init(self._grouped())
            self._set_version(_USER_VERSION)
        self.completion.update_frecency()

        self.create_index('HistoryIndex', 'url')
        self.create_index('HistoryAtimeIndex', 'atime')
        self._contains_query = self.contains_query('url')
        self._between_query = sql.Query('SELECT * FROM History '
                                        'where not redirect '
                                        'and not url like "qute://%" '
                                        'and atime > :earliest '
                                        'and atime <= :latest '
                                        'ORDER BY atime desc')

        self._before_query = sql.Query('SELECT * FROM History '
                                       'where not redirect '
                                       'and not url like "qute://%" '
                                       'and atime <= :latest '
                                       'ORDER BY atime desc '
                                       'limit :limit offset :offset')

        config.instance.changed.connect(self._on_config_changed)

    def __repr__(self):
        return utils.get_repr(self, length=len(self))

    def __contains__(self, url):
        return self._contains_query.run(val=url).value()

    @config.change_filter('completion.web_history.exclude')
    def _on_config_changed(self):
        self._metainfo['force_rebuild'] = True

    @contextlib.contextmanager
    def _handle_sql_errors(self):
        try:
            yield
        except sql.KnownError as e:
            message.error("Failed to write history: {}".format(e.text()))

    def _grouped(self):
        q = sql.Query("SELECT "
                      "    url, "
                      "    title, "
                      "    COUNT(*) AS visits, "
                      "    MIN(atime) AS first_atime, "
                      "    MAX(atime) AS last_atime "
                      "FROM History "
                      "WHERE NOT redirect AND url NOT LIKE 'qute://back%' "
                      "GROUP BY url ORDER BY atime asc")

        return list(q.run())

    def get_recent(self):
        """Get the most recent history entries."""
        return self.select(sort_by='atime', sort_order='desc', limit=100)

    def entries_between(self, earliest, latest):
        """Iterate non-redirect, non-qute entries between two timestamps.

        Args:
            earliest: Omit timestamps earlier than this.
            latest: Omit timestamps later than this.
        """
        self._between_query.run(earliest=earliest, latest=latest)
        return iter(self._between_query)

    def entries_before(self, latest, limit, offset):
        """Iterate non-redirect, non-qute entries occurring before a timestamp.

        Args:
            latest: Omit timestamps more recent than this.
            limit: Max number of entries to include.
            offset: Number of entries to skip.
        """
        self._before_query.run(latest=latest, limit=limit, offset=offset)
        return iter(self._before_query)

    def clear(self):
        """Clear all browsing history."""
        with self._handle_sql_errors():
            self.delete_all()
            self.completion.delete_all()
        self.history_cleared.emit()
        self._last_url = None

    def delete_url(self, url):
        """Remove all history entries with the given url.

        Args:
            url: URL string to delete.
        """
        qurl = QUrl(url)
        qtutils.ensure_valid(qurl)
        self.delete('url', self._format_url(qurl))
        self.completion.delete(qurl)
        if self._last_url == url:
            self._last_url = None
        self.url_cleared.emit(qurl)

    @pyqtSlot(QUrl, QUrl, str)
    def add_from_tab(self, url, requested_url, title):
        """Add a new history entry as slot, called from a BrowserTab."""
        if any(url.scheme() in ('data', 'view-source') or
               (url.scheme(), url.host()) == ('qute', 'back')
               for url in (url, requested_url)):
            return
        if url.isEmpty():
            # things set via setHtml
            return

        no_formatting = QUrl.UrlFormattingOption(0)
        if (requested_url.isValid() and
                not requested_url.matches(url, no_formatting)):
            # If the url of the page is different than the url of the link
            # originally clicked, save them both.
            self.add_url(requested_url, title, redirect=True)
        if url != self._last_url:
            self.add_url(url, title)
            self._last_url = url

    def add_url(self, url, title="", *, redirect=False, atime=None):
        """Called via add_from_tab when a URL should be added to the history.

        Args:
            url: A url (as QUrl) to add to the history.
            redirect: Whether the entry was redirected to another URL
                      (hidden in completion)
            atime: Override the atime used to add the entry
        """
        if not url.isValid():
            log.misc.warning("Ignoring invalid URL being added to history")
            return

        if 'no-sql-history' in objects.debug_flags:
            return

        atime = int(atime) if (atime is not None) else int(time.time())

        with self._handle_sql_errors():
            self.insert({'url': self._format_url(url),
                         'title': title,
                         'atime': atime,
                         'redirect': redirect})

            if not redirect:
                self.completion.add({'url': url, 'title': title,
                                     'atime': atime})

    def _format_url(self, url):
        return url.toString(QUrl.RemovePassword | QUrl.FullyEncoded)

    def _get_version(self):
        return sql.Query('pragma user_version').run().value()

    def _set_version(self, ver):
        sql.Query('pragma user_version = {}'.format(ver)).run()


@cmdutils.register()
def history_clear(force=False):
    """Clear all browsing history.

    Note this only clears the global history
    (e.g. `~/.local/share/qutebrowser/history` on Linux) but not cookies,
    the back/forward history of a tab, cache or other persistent data.

    Args:
        force: Don't ask for confirmation.
    """
    if force:
        web_history.clear()
    else:
        message.confirm_async(yes_action=web_history.clear,
                              title="Clear all browsing history?")


@cmdutils.register(debug=True)
def debug_dump_history(dest):
    """Dump the history to a file in the old pre-SQL format.

    Args:
        dest: Where to write the file to.
    """
    dest = os.path.expanduser(dest)

    lines = ('{}{} {} {}'.format(int(x.atime),
                                 '-r' * x.redirect,
                                 x.url,
                                 x.title)
             for x in web_history.select(sort_by='atime',
                                         sort_order='asc'))

    try:
        with open(dest, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        message.info("Dumped history to {}".format(dest))
    except OSError as e:
        raise cmdutils.CommandError('Could not write history: {}'
                                    .format(e))

    def _get_version(self):
        return sql.Query('pragma user_version').run().value()

    def _set_version(self, ver):
        sql.Query('pragma user_version = {}'.format(ver)).run()


def history_db_path():
    return os.path.join(standarddir.data(), 'history.sqlite')


def init(parent=None):
    """Initialize the web history.

    Args:
        parent: The parent to use for WebHistory.
    """
    global web_history
    sql.open_db(history_db_path())
    web_history = WebHistory(parent=parent)

    if objects.backend == usertypes.Backend.QtWebKit:  # pragma: no cover
        from qutebrowser.browser.webkit import webkithistory
        webkithistory.init(web_history)
