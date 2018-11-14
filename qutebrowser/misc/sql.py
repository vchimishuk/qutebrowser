# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2016-2018 Ryan Roden-Corrent (rcorre) <ryan@rcorre.net>
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

"""Provides access to an in-memory sqlite database."""

import collections

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtSql import QSqlDatabase, QSqlQuery, QSqlError

from qutebrowser.utils import log, debug


class SqliteErrorCode:

    """Error codes as used by sqlite.

    See https://sqlite.org/rescode.html - note we only define the codes we use
    in qutebrowser here.
    """

    UNKNOWN = '-1'
    BUSY = '5'  # database is locked
    READONLY = '8'  # attempt to write a readonly database
    IOERR = '10'  # disk I/O error
    CORRUPT = '11'  # database disk image is malformed
    FULL = '13'  # database or disk is full
    CANTOPEN = '14'  # unable to open database file
    CONSTRAINT = '19'  # UNIQUE constraint failed


class SqlError(Exception):

    """Base class for all SQL related errors."""

    def __init__(self, msg, error=None):
        super().__init__(msg)
        self.error = error

    def text(self):
        """Get a short text description of the error.

        This is a string suitable to show to the user as error message.
        """
        if self.error is None:
            return str(self)
        else:
            return self.error.databaseText()


class SqlEnvironmentError(SqlError):

    """Raised on an error interacting with the SQL database.

    This is raised in conditions resulting from the environment (like a full
    disk or I/O errors), where qutebrowser isn't to blame.
    """

    pass


class SqlBugError(SqlError):

    """Raised on an error interacting with the SQL database.

    This is raised for errors resulting from a qutebrowser bug.
    """

    pass


def raise_sqlite_error(msg, error):
    """Raise either a SqlBugError or SqlEnvironmentError."""
    error_code = error.nativeErrorCode()
    database_text = error.databaseText()
    driver_text = error.driverText()

    log.sql.debug("SQL error:")
    log.sql.debug("type: {}".format(
        debug.qenum_key(QSqlError, error.type())))
    log.sql.debug("database text: {}".format(database_text))
    log.sql.debug("driver text: {}".format(driver_text))
    log.sql.debug("error code: {}".format(error_code))

    environmental_errors = [
        SqliteErrorCode.BUSY,
        SqliteErrorCode.READONLY,
        SqliteErrorCode.IOERR,
        SqliteErrorCode.CORRUPT,
        SqliteErrorCode.FULL,
        SqliteErrorCode.CANTOPEN,
    ]

    # WORKAROUND for https://bugreports.qt.io/browse/QTBUG-70506
    # We don't know what the actual error was, but let's assume it's not us to
    # blame... Usually this is something like an unreadable database file.
    qtbug_70506 = (error_code == SqliteErrorCode.UNKNOWN and
                   driver_text == "Error opening database" and
                   database_text == "out of memory")

    if error_code in environmental_errors or qtbug_70506:
        raise SqlEnvironmentError(msg, error)
    else:
        raise SqlBugError(msg, error)


def open_db(db_path, conn_name=None):
    """Initialize the SQL database connection."""
    if conn_name:
        database = QSqlDatabase.addDatabase('QSQLITE', conn_name)
    else:
        database = QSqlDatabase.addDatabase('QSQLITE')
    if not database.isValid():
        raise SqlEnvironmentError('Failed to add database. Are sqlite and Qt '
                                  'sqlite support installed?')
    database.setDatabaseName(db_path)
    if not database.open():
        error = database.lastError()
        msg = "Failed to open sqlite database at {}: {}".format(db_path,
                                                                error.text())
        raise_sqlite_error(msg, error)

    # Enable write-ahead-logging and reduce disk write frequency
    # see https://sqlite.org/pragma.html and issues #2930 and #3507
    Query("PRAGMA journal_mode=WAL", db=database).run()
    Query("PRAGMA synchronous=NORMAL", db=database).run()

    return database


def close_db(name):
    """Close the SQL connection."""
    QSqlDatabase.removeDatabase(name)


def version():
    """Return the sqlite version string."""
    try:
        name = ':memory:'
        db = open_db(name, 'version')
        try:
            return Query("select sqlite_version()", db=db).run().value()
        finally:
            del db
            close_db(name)
    except SqlEnvironmentError as e:
        return 'UNAVAILABLE ({})'.format(e)


class Query:

    """A prepared SQL Query."""

    def __init__(self, querystr, forward_only=True, db=None):
        """Prepare a new sql query.

        Args:
            querystr: String to prepare query from.
            forward_only: Optimization for queries that will only step forward.
                          Must be false for completion queries.
            db: QSqlDatabase database object to use or default if None.
        """
        self._db = db or QSqlDatabase.database()
        self.query = QSqlQuery(self._db)

        log.sql.debug('Preparing SQL query: "{}"'.format(querystr))
        ok = self.query.prepare(querystr)
        self._check_ok('prepare', ok)
        self.query.setForwardOnly(forward_only)

    def __iter__(self):
        if not self.query.isActive():
            raise SqlBugError("Cannot iterate inactive query")
        rec = self.query.record()
        fields = [rec.fieldName(i) for i in range(rec.count())]
        rowtype = collections.namedtuple('ResultRow', fields)

        while self.query.next():
            rec = self.query.record()
            yield rowtype(*[rec.value(i) for i in range(rec.count())])

    def _check_ok(self, step, ok):
        if not ok:
            query = self.query.lastQuery()
            error = self.query.lastError()
            msg = 'Failed to {} query "{}": "{}"'.format(step, query,
                                                         error.text())
            raise_sqlite_error(msg, error)

    def _bind_values(self, values):
        for key, val in values.items():
            self.query.bindValue(':{}'.format(key), val)
        if any(val is None for val in self.bound_values().values()):
            raise SqlBugError("Missing bound values!")

    def run(self, **values):
        """Execute the prepared query."""
        log.sql.debug('Running SQL query: "{}"'.format(
            self.query.lastQuery()))

        self._bind_values(values)
        log.sql.debug('query bindings: {}'.format(self.bound_values()))

        ok = self.query.exec_()
        self._check_ok('exec', ok)

        return self

    def run_batch(self, values):
        """Execute the query in batch mode."""
        log.sql.debug('Running SQL query (batch): "{}"'.format(
            self.query.lastQuery()))

        self._bind_values(values)

        ok = self._db.transaction()
        self._check_ok('transaction', ok)

        ok = self.query.execBatch()
        try:
            self._check_ok('execBatch', ok)
        except SqlError:
            # Not checking the return value here, as we're failing anyways...
            self._db.rollback()
            raise

        ok = self._db.commit()
        self._check_ok('commit', ok)

    def value(self):
        """Return the result of a single-value query (e.g. an EXISTS)."""
        if not self.query.next():
            raise SqlBugError("No result for single-result query")
        return self.query.record().value(0)

    def rows_affected(self):
        return self.query.numRowsAffected()

    def bound_values(self):
        return self.query.boundValues()


class SqlTable(QObject):

    """Interface to a sql table.

    Attributes:
        _name: Name of the SQL table this wraps.

    Signals:
        changed: Emitted when the table is modified.
    """

    changed = pyqtSignal()

    def __init__(self, name, fields, constraints=None, parent=None, db=None):
        """Create a new table in the sql database.

        Does nothing if the table already exists.

        Args:
            name: Name of the table.
            fields: A list of field names.
            constraints: A dict mapping field names to constraint strings.
            db: QSqlDatabase database object to use or default if None.
        """
        super().__init__(parent)
        self._name = name
        self._db = db or QSqlDatabase.database()

        constraints = constraints or {}
        column_defs = ['{} {}'.format(field, constraints.get(field, ''))
                       for field in fields]
        s = "CREATE TABLE IF NOT EXISTS {name} ({column_defs})".format(
            name=name, column_defs=', '.join(column_defs))
        Query(s, db=self._db).run()

    def create_index(self, name, field, unique=False):
        """Create an index over this table.

        Args:
            name: Name of the index, should be unique.
            field: Name of the field to index.
        """
        tpl = "CREATE {uniq} INDEX IF NOT EXISTS {name} ON {table} ({field})"
        s = tpl.format(uniq='UNIQUE' if unique else '',
                       name=name, table=self._name, field=field)
        Query(s, db=self._db).run()

    def __iter__(self):
        """Iterate rows in the table."""
        q = Query("SELECT * FROM {table}".format(table=self._name), db=self._db)
        q.run()
        return iter(q)

    def contains_query(self, field):
        """Return a prepared query that checks for the existence of an item.

        Args:
            field: Field to match.
        """
        s = "SELECT EXISTS(SELECT * FROM {table} WHERE {field} = :val)".format(
            table=self._name, field=field)
        return Query(s, db=self._db)

    def __len__(self):
        """Return the count of rows in the table."""
        q = Query("SELECT count(*) FROM {table}".format(table=self._name),
                  db=self._db)
        q.run()
        return q.value()

    def delete(self, field, value):
        """Remove all rows for which `field` equals `value`.

        Args:
            field: Field to use as the key.
            value: Key value to delete.

        Return:
            The number of rows deleted.
        """
        s = "DELETE FROM {table} where {field} = :val".format(table=self._name,
                                                              field=field)
        q = Query(s, db=self._db)
        q.run(val=value)
        if not q.rows_affected():
            raise KeyError('No row with {} = "{}"'.format(field, value))
        self.changed.emit()

    def _insert_query(self, values, replace=False, ignore=False):
        params = ', '.join(':{}'.format(key) for key in values)
        verb = 'INSERT'
        if replace:
            verb += ' OR REPLACE'
        elif ignore:
            verb += ' OR IGNORE'
        s = "{verb} INTO {table} ({columns}) values ({params})".format(
            verb=verb, table=self._name, columns=', '.join(values),
            params=params)

        return Query(s, db=self._db)

    def insert(self, values, replace=False):
        """Append a row to the table.

        Args:
            values: A dict with a value to insert for each field name.
            replace: If set, replace existing values.
        """
        self._insert_query(values, replace).run(**values)
        self.changed.emit()

    def insert_batch(self, values, replace=False):
        """Performantly append multiple rows to the table.

        Args:
            values: A dict with a list of values to insert for each field name.
            replace: If true, overwrite rows with a primary key match.
        """
        self._insert_query(values, replace).run_batch(values)
        self.changed.emit()

    def update(self, update, where, escape=True):
        """Execute update rows statement.

        Args:
            update: column:value dict with new values to set
            where: column:value dict for filtering
            escape: enable new values SQL-escaping
        """
        if escape:
            u = ', '.join(('{} = :{}'.format(k, k) for k in update.keys()))
        else:
            u = ', '.join(('{} = {}'.format(k, v) for k, v in update.items()))

        s = 'UPDATE {} SET {}'.format(self._name, u)
        if where:
            w = ' AND '.join(('{} = :w_{}'.format(f, f) for f in where.keys()))
            s += ' WHERE {}'.format(w)

        p = update.copy()
        p.update({'w_' + k: v for k, v in where.items()})

        Query(s, db=self._db).run(**p)
        self.changed.emit()

    def upsert(self, values, index, update, escape=True):
        """
        Insert or update row if it is alredy exists.

        Args:
            values: column:value dict used for insert operation
            index: column name used as a primary index
            update: column:value dict used for update operation
            escape: enable SQL-escaping for update operation
        """
        # TODO: ON CONFLICT can be used after updating to SQLite v3.24.0.
        q = self._insert_query(values, ignore=True)
        q.run(**values)

        if not q.rows_affected() and update:
            self.update(update, {index: values[index]}, escape)
        else:
            self.changed.emit()

    def delete_all(self):
        """Remove all rows from the table."""
        Query("DELETE FROM {table}".format(table=self._name), db=self._db).run()
        self.changed.emit()

    def select(self, sort_by, sort_order, limit=-1):
        """Prepare, run, and return a select statement on this table.

        Args:
            sort_by: name of column to sort by.
            sort_order: 'asc' or 'desc'.
            limit: max number of rows in result, defaults to -1 (unlimited).

        Return: A prepared and executed select query.
        """
        q = Query("SELECT * FROM {table} ORDER BY {sort_by} {sort_order} "
                  "LIMIT :limit"
                  .format(table=self._name, sort_by=sort_by,
                          sort_order=sort_order),
                  db=self._db)
        q.run(limit=limit)
        return q
