# Copyright 2019-2021 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
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
# along with qutebrowser.  If not, see <https://www.gnu.org/licenses/>.

import pytest
webview = pytest.importorskip('qutebrowser.browser.webkit.webview')


@pytest.fixture
def real_webview(webkit_tab, qtbot):
    wv = webview.WebView(win_id=0, tab_id=0, tab=webkit_tab, private=False)
    qtbot.add_widget(wv)
    return wv


def test_background_color_none(config_stub, real_webview):
    config_stub.val.colors.webpage.bg = None
