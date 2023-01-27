import inspect
import json
import re
from typing import Callable, List, Optional

import lyricsgenius
import upnpclient
from upnpclient.soap import SOAPError
import xml
import xmltodict

from vibin import VibinError
import vibin.mediasources as mediasources
from vibin.mediasources import MediaSource
from vibin.models import Album, Track
import vibin.streamers as streamers
from vibin.streamers import Streamer
from .logger import logger


class Vibin:
    def __init__(
            self,
            streamer: Optional[str] = None,
            media: Optional[str] = None,
            discovery_timeout: int = 5,
            subscribe_callback_base: Optional[str] = None,
            on_streamer_websocket_update=None,
    ):
        logger.info("Initializing Vibin")

        self._current_streamer: Optional[Streamer] = None
        self._current_media_source: Optional[MediaSource] = None

        # Callables that want to be called (with all current state vars as
        # stringified JSON) whenever the state vars are updated.
        self._on_state_vars_update_handlers: List[Callable[[str], None]] = []

        # TODO: Improve this hacked-in support for websocket updates.
        self._on_websocket_update_handlers: List[Callable[[str, str], None]] = []

        logger.info("Discovering devices...")
        devices = upnpclient.discover(timeout=discovery_timeout)

        for device in devices:
            logger.info(
                f'Found: {device.model_name} ("{device.friendly_name}")'
            )

        self._determine_streamer(devices, streamer, subscribe_callback_base)
        self._determine_media_source(devices, media)

        self._current_streamer.register_media_source(
            self._current_media_source
        )

        self._last_played_id = None

        try:
            self._genius = lyricsgenius.Genius()
        except TypeError:
            self._genius = None

        # TODO: This could be a form of memory-capped cache where the oldest-
        #   accessed entries are removed first.
        self._lyrics_cache = {}

    def _determine_streamer(
            self, devices, streamer_name, subscribe_callback_base
    ):
        # Build a map (device model name to Streamer subclass) of all the
        # streamers Vibin is able to handle.
        known_streamers_by_model: dict[str, Streamer] = {}

        for name, obj in inspect.getmembers(streamers):
            if inspect.isclass(obj) and issubclass(obj, Streamer):
                known_streamers_by_model[obj.model_name] = obj

        # Build a list of streamer devices that Vibin can handle.
        streamer_devices: list[upnpclient.Device] = [
            device
            for device in devices
            if device.model_name in known_streamers_by_model
        ]

        streamer_device = None  # The streamer device we want to end up using.

        if streamer_name:
            # Caller provided a streamer name to match against. We match against
            # the device friendly names.
            streamer_device = next(
                (
                    device for device in streamer_devices
                    if device.friendly_name == streamer_name
                ), None
            )
        elif len(streamer_devices) > 0:
            # Fall back on the first streamer.
            streamer_device = streamer_devices[0]

        if not streamer_device:
            # No streamer is considered unrecoverable.
            msg = (
                f'Could not find streamer "{streamer_name}"' if streamer_name
                else "Could not find any known streamer devices"
            )
            raise VibinError(msg)

        # Create an instance of the Streamer subclass which we can use to
        # manage our streamer device.
        streamer_class = known_streamers_by_model[streamer_device.model_name]
        self._current_streamer = streamer_class(
            device=streamer_device,
            subscribe_callback_base=subscribe_callback_base,
            updates_handler=self._websocket_message_handler,
        )

        logger.info(f'Using streamer: "{self._current_streamer.name}"')

    def _determine_media_source(self, devices, media_name):
        # Build a map (device model name to MediaSource subclass) of all the
        # media sources Vibin is able to handle.
        known_media_by_model: dict[str, MediaSource] = {}

        for name, obj in inspect.getmembers(mediasources):
            if inspect.isclass(obj) and issubclass(obj, MediaSource):
                known_media_by_model[obj.model_name] = obj

        # Build a list of media source devices that Vibin can handle.
        media_devices: list[upnpclient.Device] = [
            device
            for device in devices
            if device.model_name in known_media_by_model
        ]

        media_device = None  # The media source device we want to end up using.

        if media_name:
            media_device = next(
                (
                    device for device in media_devices
                    if device.friendly_name == media_name
                ), None
            )
        elif len(media_devices) > 0:
            # Fall back on the first media source.
            media_device = media_devices[0]

        if not media_device and media_name:
            # No media source when the user specified a media source name is
            # considered unrecoverable.
            raise VibinError(f"Could not find media source {media_name}")

        # Create an instance of the MediaSource subclass which we can use to
        # manage our media device.
        media_source_class = known_media_by_model[media_device.model_name]
        self._current_media_source = media_source_class(device=media_device)

        logger.info(f'Using media source: "{self._current_media_source.name}"')

    @property
    def streamer(self):
        return self._current_streamer

    @property
    def media(self):
        return self._current_media_source

    def browse_media(self, parent_id: str = "0"):
        return self.media.children(parent_id)

    def play_album(self, album: Album):
        self.play_id(album.id)

    def play_track(self, track: Track):
        self.play_id(track.id)

    def play_id(self, id: str):
        self.streamer.play_metadata(self.media.get_metadata(id))
        self._last_played_id = id

    def modify_playlist(
            self,
            id: str,
            action:
            str = "REPLACE",
            insert_index: Optional[int] = None,
    ):
        self.streamer.play_metadata(self.media.get_metadata(id), action, insert_index)

    def pause(self):
        try:
            self.streamer.pause()
        except SOAPError as e:
            code, err = e.args
            raise VibinError(
                f"Unable to perform Pause transition: [{code}] {err}"
            )

    def play(self):
        try:
            self.streamer.play()
        except SOAPError as e:
            code, err = e.args
            raise VibinError(
                f"Unable to perform Play transition: [{code}] {err}"
            )

    def next_track(self):
        try:
            self.streamer.next_track()
        except SOAPError as e:
            code, err = e.args
            raise VibinError(
                f"Unable to perform Next transition: [{code}] {err}"
            )

    def previous_track(self):
        try:
            self.streamer.previous_track()
        except SOAPError as e:
            code, err = e.args
            raise VibinError(
                f"Unable to perform Previous transition: [{code}] {err}"
            )

    def repeat(self, enabled: Optional[bool]):
        try:
            self.streamer.repeat(enabled)
        except SOAPError as e:
            code, err = e.args
            raise VibinError(
                f"Unable to interact with Repeat setting: [{code}] {err}"
            )

    def shuffle(self, enabled: Optional[bool]):
        try:
            self.streamer.shuffle(enabled)
        except SOAPError as e:
            code, err = e.args
            raise VibinError(
                f"Unable to interact with Shuffle setting: [{code}] {err}"
            )

    def seek(self, target):
        self.streamer.seek(target)

    def transport_position(self):
        return self.streamer.transport_position()

    def transport_actions(self):
        return self.streamer.transport_actions()

    def transport_state(self) -> streamers.TransportState:
        return self.streamer.transport_state()

    def transport_status(self) -> str:
        return self.streamer.transport_status()

    # TODO: Consider improving this eventing system. Currently it only allows
    #   the streamer to subscribe to events; and when a new event comes in,
    #   it checks the event's service name against all the streamers
    #   subscriptions. It might be better to allow multiple streamer/media/etc
    #   objects to register event handlers with Vibin.

    def subscribe(self):
        self.streamer.subscribe()

    @property
    def state_vars(self):
        # TODO: Do a pass at redefining the shape of state_vars. It should
        #   include:
        #   * Standard keys shared across all streamers/media (audience: any
        #     client which wants to be device-agnostic). This will require some
        #     well-defined keys in some sort of device interface definition.
        #   * All streamer- and media-specific data (audience: any client which
        #     is OK with understanding device-specific data).
        all_vars = {
            "streamer_name": self.streamer.name,
            "media_source_name": self.media.name,
            self.streamer.name: self.streamer.state_vars,
            "vibin": {
                "last_played_id": self._last_played_id,
                self.streamer.name: self.streamer.vibin_vars
            }
        }

        return all_vars

    @property
    def system_state(self):
        return {
            "streamer": self.streamer.system_state,
        }

    @property
    def play_state(self):
        return self.streamer.play_state

    # TODO: Fix handling of state_vars (UPNP) and updates (Websocket) to be
    #   more consistent. One option: more clearly configure handling of UPNP
    #   subscriptions and Websocket events from the streamer; both can be
    #   passed back to the client on the same Vibin->Client websocket
    #   connection, perhaps with different message type identifiers.

    def lyrics_for_track(self, track_id):
        if track_id in self._lyrics_cache:
            return self._lyrics_cache[track_id]

        if not self._genius:
            return

        try:
            track_info = xmltodict.parse(self.media.get_metadata(track_id))

            artist = track_info["DIDL-Lite"]["item"]["dc:creator"]
            title = track_info["DIDL-Lite"]["item"]["dc:title"]

            song = self._genius.search_song(title=title, artist=artist)

            if song is None:
                return None

            # Munge the lyrics into a new shape. Currently they're one long
            # string, where chunks (choruses, verses, etc) are separated by two
            # newlines. Each chunk may or may not have a header of sorts, which
            # looks like "[Header]". The goal is to create something like:
            #
            # [
            #     {
            #         "header": "Verse 1",
            #         "body": [
            #             "Line 1",
            #             "Line 2",
            #             "Line 3",
            #         ],
            #     },
            #     {
            #         "header": "Verse 2",
            #         "body": [
            #             "Line 1",
            #             "Line 2",
            #             "Line 3",
            #         ],
            #     },
            # ]

            chunks_as_strings = song.lyrics.split("\n\n")

            # The lyrics scraper prepends the first line of lyrics with
            # "<song title>Lyrics", so we remove that if we see it. This is
            # flaky at best.
            chunks_as_strings[0] = \
                re.sub(r"^.*Lyrics", "", chunks_as_strings[0])

            # The scraper also might append "3Embed" to the last line.
            chunks_as_strings[-1] = re.sub(
                r"\d+Embed$", "", chunks_as_strings[-1]
            )

            chunks_as_arrays = \
                [chunk.split("\n") for chunk in chunks_as_strings]

            results = []

            for chunk in chunks_as_arrays:
                chunk_header = re.match(r"^\[([^\[\]]+)\]$", chunk[0])
                if chunk_header:
                    results.append({
                        "header": chunk_header.group(1),
                        "body": chunk[1:],
                    })
                else:
                    results.append({
                        "header": None,
                        "body": chunk,
                    })

            self._lyrics_cache[track_id] = results

            return results
        except xml.parsers.expat.ExpatError as e:
            logger.error(
                f"Could not convert XML to JSON for track: {track_id}: {e}"
            )
        except (KeyError, IndexError) as e:
            logger.error(
                f"Could not extract track details for lyrics lookup for " +
                f"track {track_id}: {e}"
            )

        return None

    def on_state_vars_update(self, handler):
        self._on_state_vars_update_handlers.append(handler)

    # NOTE: Intended use: For an external entity to register interest in
    #   receiving websocket messages as they come in.
    def on_websocket_update(self, handler):
        self._on_websocket_update_handlers.append(handler)

    def upnp_event(self, service_name: str, event: str):
        # Extract the event.

        # Pass event to the streamer.
        if self.streamer.subscriptions:
            subscribed_service_names = [
                service.name for service in self.streamer.subscriptions.keys()
            ]

            if service_name in subscribed_service_names:
                self.streamer.on_event(service_name, event)

            # Send state vars to interested recipients.
            for handler in self._on_state_vars_update_handlers:
                handler(json.dumps(self.state_vars))

    def _websocket_message_handler(self, message_type: str, data: str):
        for handler in self._on_websocket_update_handlers:
            handler(message_type, data)

    def shutdown(self):
        logger.info("Vibin is shutting down")

        if self._current_streamer:
            logger.info(f"Disconnecting from {self._current_streamer.name}")
            self._current_streamer.disconnect()

        logger.info("Vibin shutdown complete")
