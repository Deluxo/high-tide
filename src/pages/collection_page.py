# collection_page.py
#
# Copyright 2024 Nokse22
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from gettext import gettext as _

from tidalapi.types import AlbumOrder, ArtistOrder, ItemOrder, OrderDirection

from ..lib import utils
from .page import Page


class HTCollectionPage(Page):
    """A page to display the collection (the user's library)"""

    __gtype_name__ = "HTCollectionPage"

    def _load_async(self) -> None: ...

    @staticmethod
    def _create_paginated_loader(fetch_page, items_cache=None, **fetch_options):
        def load_page(limit: int = 50, offset: int = 0):
            page_items = fetch_page(
                limit=limit, offset=offset, **fetch_options
            )
            if items_cache is not None:
                known_item_ids = {
                    getattr(item, "id", None) for item in items_cache
                }
                items_cache.extend(
                    item
                    for item in page_items
                    if getattr(item, "id", None) not in known_item_ids
                )
            return page_items

        return load_page

    def _load_finish(self) -> None:
        self.set_tag("collection")
        self.set_title(_("Collection"))

        user = utils.session.user
        desc = OrderDirection.Descending

        carousels = [
            (
                _("My Mixes and Radios"),
                utils.favourite_mixes,
                user.favorites.mixes,
                {},
            ),
            (
                _("Playlists"),
                utils.playlist_and_favorite_playlists,
                user.playlist_and_favorite_playlists,
                {},
            ),
            (
                _("Albums"),
                utils.favourite_albums,
                user.favorites.albums,
                {"order": AlbumOrder.DateAdded, "order_direction": desc},
            ),
            (
                _("Tracks"),
                utils.favourite_tracks,
                user.favorites.tracks,
                {"order": ItemOrder.Date, "order_direction": desc},
            ),
            (
                _("Artists"),
                utils.favourite_artists,
                user.favorites.artists,
                {"order": ArtistOrder.DateAdded, "order_direction": desc},
            ),
        ]

        for title, items_cache, fetch_page, fetch_options in carousels:
            self.new_carousel_for(
                title,
                items_cache,
                self._create_paginated_loader(
                    fetch_page, items_cache, **fetch_options
                ),
            )
