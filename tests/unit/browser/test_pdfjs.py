# SPDX-FileCopyrightText: Daniel Schadt
#
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import pathlib
import unittest.mock
import os.path

import pytest
from qutebrowser.qt.core import QUrl

from qutebrowser.browser import pdfjs
from qutebrowser.utils import urlmatch, utils
from qutebrowser.misc import objects


pytestmark = [pytest.mark.usefixtures('data_tmpdir')]


@pytest.mark.parametrize('available, snippet', [
    (True, '<title>PDF.js viewer</title>'),
    (False, '<h1>No pdf.js installation found</h1>'),
    ('force', 'fake PDF.js'),
])
def test_generate_pdfjs_page(available, snippet, monkeypatch):
    if available == 'force':
        monkeypatch.setattr(pdfjs, 'is_available', lambda: True)
        monkeypatch.setattr(pdfjs, 'get_pdfjs_res',
                            lambda filename: b'fake PDF.js')
    elif available:
        if not pdfjs.is_available():
            pytest.skip("PDF.js unavailable")
        monkeypatch.setattr(pdfjs, 'is_available', lambda: True)
    else:
        monkeypatch.setattr(pdfjs, 'is_available', lambda: False)

    content = pdfjs.generate_pdfjs_page('example.pdf', QUrl())
    print(content)
    assert snippet in content


# Note that we got double protection, once because we use QUrl.ComponentFormattingOption.FullyEncoded and
# because we use qutebrowser.utils.javascript.to_js. Characters like " are
# already replaced by QUrl.
@pytest.mark.parametrize('filename, expected', [
    ('foo.bar', "foo.bar"),
    ('foo"bar', "foo%22bar"),
    ('foo\0bar', 'foo%00bar'),
    ('foobar");alert("attack!");',
     'foobar%22);alert(%22attack!%22);'),
])
def test_generate_pdfjs_script(filename, expected):
    expected_open = 'open("qute://pdfjs/file?filename={}");'.format(expected)
    actual = pdfjs._generate_pdfjs_script(filename)
    assert expected_open in actual
    assert 'PDFView' in actual


class TestResources:

    @pytest.fixture
    def read_system_mock(self, mocker):
        return mocker.patch.object(pdfjs, '_read_from_system', autospec=True)

    @pytest.fixture
    def read_file_mock(self, mocker):
        return mocker.patch.object(pdfjs.resources, 'read_file_binary', autospec=True)

    def test_get_pdfjs_res_system(self, read_system_mock):
        read_system_mock.return_value = (b'content', 'path')

        assert pdfjs.get_pdfjs_res_and_path('web/test') == (b'content', 'path')
        assert pdfjs.get_pdfjs_res('web/test') == b'content'

        read_system_mock.assert_called_with('/usr/share/pdf.js/',
                                            ['web/test', 'test'])

    @pytest.mark.parametrize("with_system", [True, False])
    def test_get_pdfjs_res_bundled(
        self,
        read_system_mock: unittest.mock.Mock,
        read_file_mock: unittest.mock.Mock,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        with_system: bool,
    ) -> None:
        read_system_mock.return_value = (None, None)
        read_file_mock.return_value = b'content'
        if not with_system:
            monkeypatch.setattr(objects, 'debug_flags', {'no-system-pdfjs'})

        assert pdfjs.get_pdfjs_res_and_path('web/test') == (b'content', None)
        assert pdfjs.get_pdfjs_res('web/test') == b'content'

        paths = {call.args[0] for call in read_system_mock.mock_calls}
        expected_paths = {
            str(tmp_path / 'data' / 'pdfjs'),
            # hardcoded for --temp-basedir
            os.path.expanduser('~/.local/share/qutebrowser/pdfjs/')
        }
        assert expected_paths.issubset(paths)
        if not with_system:
            assert '/usr/share/pdf.js/' not in paths

    def test_get_pdfjs_res_not_found(self, read_system_mock, read_file_mock,
                                     caplog):
        read_system_mock.return_value = (None, None)
        read_file_mock.side_effect = FileNotFoundError

        with pytest.raises(pdfjs.PDFJSNotFound,
                           match="Path 'web/test' not found"):
            pdfjs.get_pdfjs_res_and_path('web/test')

        assert not caplog.records

    def test_get_pdfjs_res_oserror(self, read_system_mock, read_file_mock,
                                   caplog):
        read_system_mock.return_value = (None, None)
        read_file_mock.side_effect = OSError("Message")

        with caplog.at_level(logging.WARNING):
            with pytest.raises(pdfjs.PDFJSNotFound,
                               match="Path 'web/test' not found"):
                pdfjs.get_pdfjs_res_and_path('web/test')

        expected = 'OSError while reading PDF.js file: Message'
        assert caplog.messages == [expected]

    def test_broken_installation(self, data_tmpdir, tmpdir, monkeypatch,
                                 read_file_mock):
        """Make sure we don't crash with a broken local installation."""
        monkeypatch.setattr(pdfjs, '_SYSTEM_PATHS', [])
        monkeypatch.setattr(pdfjs.os.path, 'expanduser',
                            lambda _in: tmpdir / 'fallback')
        read_file_mock.side_effect = FileNotFoundError

        (data_tmpdir / 'pdfjs' / 'pdf.js').ensure()  # But no viewer.html

        content = pdfjs.generate_pdfjs_page('example.pdf', QUrl())
        assert '<h1>No pdf.js installation found</h1>' in content


@pytest.mark.parametrize('path, expected', [
    ('web/viewer.js', 'viewer.js'),
    ('build/locale/foo.bar', 'locale/foo.bar'),
    ('viewer.js', 'viewer.js'),
    ('foo/viewer.css', 'foo/viewer.css'),
])
def test_remove_prefix(path, expected):
    assert pdfjs._remove_prefix(path) == expected


@pytest.mark.parametrize('names, expected_name', [
    (['one'], 'one'),
    (['doesnotexist', 'two'], 'two'),
    (['one', 'two'], 'one'),
    (['does', 'not', 'onexist'], None),
])
def test_read_from_system(names, expected_name, tmpdir):
    file1 = tmpdir / 'one'
    file1.write_text('text1', encoding='ascii')
    file2 = tmpdir / 'two'
    file2.write_text('text2', encoding='ascii')

    if expected_name == 'one':
        expected = (b'text1', str(file1))
    elif expected_name == 'two':
        expected = (b'text2', str(file2))
    elif expected_name is None:
        expected = (None, None)
    else:
        raise utils.Unreachable(expected_name)

    assert pdfjs._read_from_system(str(tmpdir), names) == expected


@pytest.fixture
def unreadable_file(tmpdir):
    unreadable_file = tmpdir / 'unreadable'
    unreadable_file.ensure()
    unreadable_file.chmod(0)
    if os.access(unreadable_file, os.R_OK):
        # Docker container or similar
        pytest.skip("File was still readable")

    yield unreadable_file

    unreadable_file.chmod(0o755)


def test_read_from_system_oserror(tmpdir, caplog, unreadable_file):
    expected = (None, None)
    with caplog.at_level(logging.WARNING):
        assert pdfjs._read_from_system(str(tmpdir), ['unreadable']) == expected

    assert len(caplog.records) == 1
    message = caplog.messages[0]
    assert message.startswith('OSError while reading PDF.js file:')


@pytest.mark.parametrize('available', [True, False])
def test_is_available(available, mocker):
    mock = mocker.patch.object(pdfjs, 'get_pdfjs_res', autospec=True)
    if available:
        mock.return_value = b'foo'
    else:
        mock.side_effect = pdfjs.PDFJSNotFound('build/pdf.js')

    assert pdfjs.is_available() == available


@pytest.mark.parametrize('found_file', [
    "build/pdf.js",
    "build/pdf.mjs",
])
def test_get_pdfjs_js_path(found_file: str, monkeypatch: pytest.MonkeyPatch):
    def fake_pdfjs_res(requested):
        if requested.endswith(found_file):
            return
        raise pdfjs.PDFJSNotFound(requested)

    monkeypatch.setattr(pdfjs, 'get_pdfjs_res', fake_pdfjs_res)
    assert pdfjs.get_pdfjs_js_path() == found_file


def test_get_pdfjs_js_path_none(monkeypatch: pytest.MonkeyPatch):
    def fake_pdfjs_res(requested):
        raise pdfjs.PDFJSNotFound(requested)

    monkeypatch.setattr(pdfjs, 'get_pdfjs_res', fake_pdfjs_res)

    with pytest.raises(
        pdfjs.PDFJSNotFound,
        match="Path 'build/pdf.js or build/pdf.mjs' not found"
    ):
        pdfjs.get_pdfjs_js_path()


@pytest.mark.parametrize('mimetype, url, enabled, expected', [
    # PDF files
    ('application/pdf', 'http://www.example.com', True, True),
    ('application/x-pdf', 'http://www.example.com', True, True),
    # Not a PDF
    ('application/octet-stream', 'http://www.example.com', True, False),
    # PDF.js disabled
    ('application/pdf', 'http://www.example.com', False, False),
    # Download button in PDF.js
    ('application/pdf', 'blob:qute%3A///b45250b3', True, False),
])
def test_should_use_pdfjs(mimetype, url, enabled, expected, config_stub):
    config_stub.val.content.pdfjs = enabled
    assert pdfjs.should_use_pdfjs(mimetype, QUrl(url)) == expected


@pytest.mark.parametrize('url, expected', [
    ('http://example.com', True),
    ('http://example.org', False),
])
def test_should_use_pdfjs_url_pattern(config_stub, url, expected):
    config_stub.val.content.pdfjs = False
    pattern = urlmatch.UrlPattern('http://example.com')
    config_stub.set_obj('content.pdfjs', True, pattern=pattern)
    assert pdfjs.should_use_pdfjs('application/pdf', QUrl(url)) == expected


def test_get_main_url():
    expected = QUrl('qute://pdfjs/web/viewer.html?filename=hello?world.pdf&'
                    'file=&source=http://a.com/hello?world.pdf')
    original_url = QUrl('http://a.com/hello?world.pdf')
    assert pdfjs.get_main_url('hello?world.pdf', original_url) == expected
