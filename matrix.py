# -*- coding: utf-8 -*-

# Weechat Matrix Protocol Script
# Copyright © 2018 Damir Jelić <poljar@termina.org.uk>
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

from __future__ import unicode_literals

import json
import socket
import ssl
import time
import datetime
import pprint

# pylint: disable=redefined-builtin
from builtins import str

from operator import itemgetter

# pylint: disable=unused-import
from typing import (List, Set, Dict, Tuple, Text, Optional, AnyStr, Deque, Any)

from matrix import colors
from matrix.utf import utf8_decode
from matrix.http import HttpResponse
from matrix.api import MatrixMessage, MessageType
from matrix.server import MatrixServer
from matrix.socket import disconnect, send_or_queue, send


# Weechat searches for the registered callbacks in the global scope, import the
# callbacks here so weechat can find them.
from matrix.commands import (
    hook_commands,
    hook_page_up,
    matrix_command_join_cb,
    matrix_command_part_cb,
    matrix_command_invite_cb,
    matrix_command_pgup_cb,
    matrix_redact_command_cb,
    matrix_command_buf_clear_cb,
    matrix_debug_completion_cb,
    matrix_message_completion_cb
)

from matrix.utils import (
    key_from_value,
    server_buffer_prnt,
    prnt_debug,
    tags_from_line_data
)

from matrix.config import (
    DebugType,
    RedactType,
    ServerBufferType
)

import matrix.globals

W = matrix.globals.W
GLOBAL_OPTIONS = matrix.globals.OPTIONS
CONFIG = matrix.globals.CONFIG
SERVERS = matrix.globals.SERVERS


WEECHAT_SCRIPT_NAME        = "matrix"                               # type: str
WEECHAT_SCRIPT_DESCRIPTION = "matrix chat plugin"                   # type: str
WEECHAT_SCRIPT_AUTHOR      = "Damir Jelić <poljar@termina.org.uk>"  # type: str
WEECHAT_SCRIPT_VERSION     = "0.1"                                  # type: str
WEECHAT_SCRIPT_LICENSE     = "ISC"                                  # type: str


class MatrixUser:
    def __init__(self, name, display_name):
        self.name         = name          # type: str
        self.display_name = display_name  # type: str
        self.power_level  = 0             # type: int
        self.nick_color   = ""            # type: str
        self.prefix       = ""            # type: str


class MatrixRoom:
    def __init__(self, room_id):
        # type: (str) -> None
        self.room_id      = room_id    # type: str
        self.alias        = room_id    # type: str
        self.topic        = ""         # type: str
        self.topic_author = ""         # type: str
        self.topic_date   = None       # type: datetime.datetime
        self.prev_batch   = ""         # type: str
        self.users        = dict()     # type: Dict[str, MatrixUser]
        self.encrypted    = False      # type: bool


@utf8_decode
def server_config_change_cb(server_name, option):
    # type: (str, weechat.config_option) -> int
    server = SERVERS[server_name]
    option_name = None

    # The function config_option_get_string() is used to get differing
    # properties from a config option, sadly it's only available in the plugin
    # API of weechat.
    option_name = key_from_value(server.options, option)
    server.update_option(option, option_name, W)

    return 1


def wrap_socket(server, file_descriptor):
    # type: (MatrixServer, int) -> socket.socket
    sock = None  # type: socket.socket

    temp_socket = socket.fromfd(
        file_descriptor,
        socket.AF_INET,
        socket.SOCK_STREAM
    )

    # For python 2.7 wrap_socket() doesn't work with sockets created from an
    # file descriptor because fromfd() doesn't return a wrapped socket, the bug
    # was fixed for python 3, more info: https://bugs.python.org/issue13942
    # pylint: disable=protected-access,unidiomatic-typecheck
    if type(temp_socket) == socket._socket.socket:
        # pylint: disable=no-member
        sock = socket._socketobject(_sock=temp_socket)
    else:
        sock = temp_socket

    try:
        ssl_socket = server.ssl_context.wrap_socket(
            sock,
            server_hostname=server.address)  # type: ssl.SSLSocket

        return ssl_socket
    # TODO add finer grained error messages with the subclass exceptions
    except ssl.SSLError as error:
        server_buffer_prnt(server, str(error))
        return None


def handle_http_response(server, message):
    # type: (MatrixServer, MatrixMessage) -> None

    assert message.response

    status_code = message.response.status

    def decode_json(server, json_string):
        try:
            return json.loads(json_string, encoding='utf-8')
        except Exception as error:
            message = ("{prefix}matrix: Error decoding json response from "
                       "server: {error}").format(
                           prefix=W.prefix("error"),
                           error=error)

            W.prnt(server.server_buffer, message)
            return None

    if status_code == 200:
        response = decode_json(server, message.response.body)

        # if not response:
        #     # Resend the message
        #     message.response = None
        #     send_or_queue(server, message)
        #     return

        matrix_handle_message(
            server,
            message.type,
            response,
            message.extra_data
        )

    # TODO handle try again response
    elif status_code == 504:
        if message.type == MessageType.SYNC:
            matrix_sync(server)

    elif status_code == 403:
        if message.type == MessageType.LOGIN:
            response = decode_json(server, message.response.body)
            reason = ("." if not response or not response["error"] else
                      ": {r}.".format(r=response["error"]))

            error_message = ("{prefix}Login error{reason}").format(
                prefix=W.prefix("error"),
                reason=reason)
            server_buffer_prnt(server, error_message)

            W.unhook(server.timer_hook)
            server.timer_hook = None

            close_socket(server)
            disconnect(server)
        elif message.type == MessageType.STATE:
            response = decode_json(server, message.response.body)
            reason = ("." if not response or not response["error"] else
                      ": {r}.".format(r=response["error"]))

            error_message = ("{prefix}Can't set state{reason}").format(
                prefix=W.prefix("network"),
                reason=reason)
            server_buffer_prnt(server, error_message)
        else:
            error_message = ("{prefix}Unhandled 403 error, please inform the "
                             "developers about this: {error}").format(
                                 prefix=W.prefix("error"),
                                 error=message.response.body)
            server_buffer_prnt(server, error_message)

    else:
        server_buffer_prnt(
            server,
            ("{prefix}Unhandled {status_code} error, please inform "
             "the developers about this.").format(
                 prefix=W.prefix("error"),
                 status_code=status_code))

        server_buffer_prnt(server, pprint.pformat(message.type))
        server_buffer_prnt(server, pprint.pformat(message.request.payload))
        server_buffer_prnt(server, pprint.pformat(message.response.body))

    creation_date = datetime.datetime.fromtimestamp(message.creation_time)
    done_time = time.time()
    info_message = ("Message of type {t} created at {c}."
                    "\nMessage lifetime information:"
                    "\n    Send delay: {s} ms"
                    "\n    Receive delay: {r} ms"
                    "\n    Handling time: {h} ms"
                    "\n    Total time: {total} ms").format(
                        t=message.type,
                        c=creation_date,
                        s=(message.send_time - message.creation_time) * 1000,
                        r=(message.receive_time - message.send_time) * 1000,
                        h=(done_time - message.receive_time) * 1000,
                        total=(done_time - message.creation_time) * 1000,)
    prnt_debug(DebugType.TIMING, server, info_message)

    return


def strip_matrix_server(string):
    # type: (str) -> str
    return string.rsplit(":", 1)[0]


def add_user_to_nicklist(buf, user):
    group_name = "999|..."

    if user.power_level >= 100:
        group_name = "000|o"
    elif user.power_level >= 50:
        group_name = "001|h"
    elif user.power_level > 0:
        group_name = "002|v"

    group = W.nicklist_search_group(buf, "", group_name)
    # TODO make it configurable so we can use a display name or user_id here
    W.nicklist_add_nick(
        buf,
        group,
        user.display_name,
        user.nick_color,
        user.prefix,
        get_prefix_color(user.prefix),
        1
    )


def matrix_create_room_buffer(server, room_id):
    # type: (MatrixServer, str) -> None
    buf = W.buffer_new(
        room_id,
        "room_input_cb",
        server.name,
        "room_close_cb",
        server.name
    )

    W.buffer_set(buf, "localvar_set_type", 'channel')
    W.buffer_set(buf, "type", 'formatted')

    W.buffer_set(buf, "localvar_set_channel", room_id)

    W.buffer_set(buf, "localvar_set_nick", server.user)

    W.buffer_set(buf, "localvar_set_server", server.name)

    short_name = strip_matrix_server(room_id)
    W.buffer_set(buf, "short_name", short_name)

    W.nicklist_add_group(buf, '', "000|o", "weechat.color.nicklist_group", 1)
    W.nicklist_add_group(buf, '', "001|h", "weechat.color.nicklist_group", 1)
    W.nicklist_add_group(buf, '', "002|v", "weechat.color.nicklist_group", 1)
    W.nicklist_add_group(buf, '', "999|...", "weechat.color.nicklist_group", 1)

    W.buffer_set(buf, "nicklist", "1")
    W.buffer_set(buf, "nicklist_display_groups", "0")

    server.buffers[room_id] = buf
    server.rooms[room_id] = MatrixRoom(room_id)


def matrix_handle_room_aliases(server, room_id, event):
    # type: (MatrixServer, str, Dict[str, Any]) -> None
    buf = server.buffers[room_id]
    room = server.rooms[room_id]

    alias = event['content']['aliases'][-1]

    if not alias:
        return

    short_name = strip_matrix_server(alias)

    room.alias = alias
    W.buffer_set(buf, "name", alias)
    W.buffer_set(buf, "short_name", short_name)
    W.buffer_set(buf, "localvar_set_channel", alias)


def matrix_handle_room_members(server, room_id, event):
    # type: (MatrixServer, str, Dict[str, Any]) -> None
    buf = server.buffers[room_id]
    room = server.rooms[room_id]

    # TODO print out a informational message
    if event['membership'] == 'join':
        # TODO set the buffer type to a channel if we have more than 2 users
        display_name = event['content']['displayname']
        full_name = event['sender']
        short_name = strip_matrix_server(full_name)[1:]

        if not display_name:
            display_name = short_name

        user = MatrixUser(short_name, display_name)

        if full_name == server.user_id:
            user.nick_color = "weechat.color.chat_nick_self"
            W.buffer_set(
                buf,
                "highlight_words",
                ",".join([full_name, user.name, user.display_name]))
        else:
            user.nick_color = W.info_get("nick_color_name", user.name)

        room.users[full_name] = user

        nick_pointer = W.nicklist_search_nick(buf, "", user.display_name)
        if not nick_pointer:
            add_user_to_nicklist(buf, user)
        else:
            # TODO we can get duplicate display names
            pass

    elif event['membership'] == 'leave':
        full_name = event['sender']
        if full_name in room.users:
            user = room.users[full_name]
            nick_pointer = W.nicklist_search_nick(buf, "", user.display_name)
            if nick_pointer:
                W.nicklist_remove_nick(buf, nick_pointer)

            del room.users[full_name]


def date_from_age(age):
    # type: (float) -> int
    now = time.time()
    date = int(now - (age / 1000))
    return date


def color_for_tags(color):
    if color == "weechat.color.chat_nick_self":
        option = W.config_get(color)
        return W.config_string(option)
    return color


def matrix_handle_room_text_message(server, room_id, event, old=False):
    # type: (MatrixServer, str, Dict[str, Any], bool) -> None
    tag = ""
    msg_author = ""
    nick_color_name = ""

    room = server.rooms[room_id]
    msg = event['content']['body']

    if 'format' in event['content'] and 'formatted_body' in event['content']:
        if event['content']['format'] == "org.matrix.custom.html":
            formatted_data = colors.html_to_formatted(
                event['content']['formatted_body'])
            msg = colors.formatted_to_weechat(W, formatted_data)

    if event['sender'] in room.users:
        user = room.users[event['sender']]
        msg_author = user.display_name
        nick_color_name = user.nick_color
    else:
        msg_author = strip_matrix_server(event['sender'])[1:]
        nick_color_name = W.info_get("nick_color_name", msg_author)

    data = "{author}\t{msg}".format(author=msg_author, msg=msg)

    event_id = event['event_id']

    msg_date = date_from_age(event['unsigned']['age'])

    # TODO if this is an initial sync tag the messages as backlog
    # TODO handle self messages from other devices
    if old:
        tag = ("nick_{a},prefix_nick_{color},matrix_id_{event_id},"
               "matrix_message,notify_message,no_log,no_highlight").format(
                   a=msg_author,
                   color=color_for_tags(nick_color_name),
                   event_id=event_id)
    else:
        tag = ("nick_{a},prefix_nick_{color},matrix_id_{event_id},"
               "matrix_message,notify_message,log1").format(
                   a=msg_author,
                   color=color_for_tags(nick_color_name),
                   event_id=event_id)

    buf = server.buffers[room_id]
    W.prnt_date_tags(buf, msg_date, tag, data)


def matrix_handle_redacted_message(server, room_id, event):
    # type: (MatrixServer, str, Dict[Any, Any]) -> None
    reason = ""
    room = server.rooms[room_id]

    # TODO check if the message is already printed out, in that case we got the
    # message a second time and a redaction event will take care of it.
    censor = event['unsigned']['redacted_because']['sender']
    nick_color_name = ""

    if censor in room.users:
        user = room.users[censor]
        nick_color_name = user.nick_color
        censor = ("{nick_color}{nick}{ncolor} {del_color}"
                  "({host_color}{full_name}{ncolor}{del_color})").format(
                      nick_color=W.color(nick_color_name),
                      nick=user.display_name,
                      ncolor=W.color("reset"),
                      del_color=W.color("chat_delimiters"),
                      host_color=W.color("chat_host"),
                      full_name=censor)
    else:
        censor = strip_matrix_server(censor)[1:]
        nick_color_name = W.info_get("nick_color_name", censor)
        censor = "{color}{censor}{ncolor}".format(
            color=W.color(nick_color_name),
            censor=censor,
            ncolor=W.color("reset"))

    if 'reason' in event['unsigned']['redacted_because']['content']:
        reason = ", reason: \"{reason}\"".format(
            reason=event['unsigned']['redacted_because']['content']['reason'])

    msg = ("{del_color}<{log_color}Message redacted by: "
           "{censor}{log_color}{reason}{del_color}>{ncolor}").format(
               del_color=W.color("chat_delimiters"),
               ncolor=W.color("reset"),
               log_color=W.color("logger.color.backlog_line"),
               censor=censor,
               reason=reason)

    msg_author = strip_matrix_server(event['sender'])[1:]

    data = "{author}\t{msg}".format(author=msg_author, msg=msg)

    event_id = event['event_id']

    msg_date = date_from_age(event['unsigned']['age'])

    tag = ("nick_{a},prefix_nick_{color},matrix_id_{event_id},"
           "matrix_message,matrix_redacted,"
           "notify_message,no_highlight").format(
               a=msg_author,
               color=color_for_tags(nick_color_name),
               event_id=event_id)

    buf = server.buffers[room_id]
    W.prnt_date_tags(buf, msg_date, tag, data)


def matrix_handle_room_messages(server, room_id, event, old=False):
    # type: (MatrixServer, str, Dict[str, Any], bool) -> None
    if event['type'] == 'm.room.message':
        if 'redacted_by' in event['unsigned']:
            matrix_handle_redacted_message(server, room_id, event)
            return

        if event['content']['msgtype'] == 'm.text':
            matrix_handle_room_text_message(server, room_id, event, old)

        # TODO handle different content types here
        else:
            message = ("{prefix}Handling of content type "
                       "{type} not implemented").format(
                           type=event['content']['msgtype'],
                           prefix=W.prefix("error"))
            W.prnt(server.server_buffer, message)


def event_id_from_tags(tags):
    # type: (List[str]) -> str
    for tag in tags:
        if tag.startswith("matrix_id"):
            return tag[10:]

    return ""


def string_strikethrough(string):
    return "".join(["{}\u0336".format(c) for c in string])


def matrix_redact_line(data, tags, event):
    reason = ""

    hdata_line_data = W.hdata_get('line_data')

    message = W.hdata_string(hdata_line_data, data, 'message')
    censor = strip_matrix_server(event['sender'])[1:]

    if 'reason' in event['content']:
        reason = ", reason: \"{reason}\"".format(
            reason=event['content']['reason'])

    redaction_msg = ("{del_color}<{log_color}Message redacted by: "
                     "{censor}{log_color}{reason}{del_color}>{ncolor}").format(
                         del_color=W.color("chat_delimiters"),
                         ncolor=W.color("reset"),
                         log_color=W.color("logger.color.backlog_line"),
                         censor=censor,
                         reason=reason)

    if GLOBAL_OPTIONS.redaction_type == RedactType.STRIKETHROUGH:
        message = string_strikethrough(message)
        message = message + " " + redaction_msg
    elif GLOBAL_OPTIONS.redaction_type == RedactType.DELETE:
        message = redaction_msg
    elif GLOBAL_OPTIONS.redaction_type == RedactType.NOTICE:
        message = message + " " + redaction_msg

    tags.append("matrix_new_redacted")

    new_data = {'tags_array': tags,
                'message': message}

    W.hdata_update(hdata_line_data, data, new_data)

    return W.WEECHAT_RC_OK


def matrix_handle_room_redaction(server, room_id, event):
    buf = server.buffers[room_id]
    event_id = event['redacts']

    own_lines = W.hdata_pointer(W.hdata_get('buffer'), buf, 'own_lines')

    if own_lines:
        hdata_line = W.hdata_get('line')

        line = W.hdata_pointer(
            W.hdata_get('lines'),
            own_lines,
            'last_line'
        )

        while line:
            data = W.hdata_pointer(hdata_line, line, 'data')

            if data:
                tags = tags_from_line_data(data)

                message_id = event_id_from_tags(tags)

                if event_id == message_id:
                    # If the message is already redacted there is nothing to do
                    if ("matrix_redacted" not in tags and
                            "matrix_new_redacted" not in tags):
                        matrix_redact_line(data, tags, event)
                    return W.WEECHAT_RC_OK

            line = W.hdata_move(hdata_line, line, -1)

    return W.WEECHAT_RC_OK


def get_prefix_for_level(level):
    # type: (int) -> str
    if level >= 100:
        return "&"
    elif level >= 50:
        return "@"
    elif level > 0:
        return "+"
    return ""


# TODO make this configurable
def get_prefix_color(prefix):
    # type: (str) -> str
    if prefix == "&":
        return "lightgreen"
    elif prefix == "@":
        return "lightgreen"
    elif prefix == "+":
        return "yellow"
    return ""


def matrix_handle_room_power_levels(server, room_id, event):
    if not event['content']['users']:
        return

    buf = server.buffers[room_id]
    room = server.rooms[room_id]

    for full_name, level in event['content']['users'].items():
        if full_name not in room.users:
            continue

        user = room.users[full_name]
        user.power_level = level
        user.prefix = get_prefix_for_level(level)

        nick_pointer = W.nicklist_search_nick(buf, "", user.display_name)
        W.nicklist_remove_nick(buf, nick_pointer)
        add_user_to_nicklist(buf, user)


def matrix_handle_room_events(server, room_id, room_events):
    # type: (MatrixServer, str, Dict[Any, Any]) -> None
    for event in room_events:
        if event['event_id'] in server.ignore_event_list:
            server.ignore_event_list.remove(event['event_id'])
            continue

        if event['type'] == 'm.room.aliases':
            matrix_handle_room_aliases(server, room_id, event)

        elif event['type'] == 'm.room.member':
            matrix_handle_room_members(server, room_id, event)

        elif event['type'] == 'm.room.message':
            matrix_handle_room_messages(server, room_id, event)

        elif event['type'] == 'm.room.topic':
            buf = server.buffers[room_id]
            room = server.rooms[room_id]
            topic = event['content']['topic']

            room.topic = topic
            room.topic_author = event['sender']

            topic_age = event['unsigned']['age']
            room.topic_date = datetime.datetime.fromtimestamp(
                time.time() - (topic_age / 1000))

            W.buffer_set(buf, "title", topic)

            nick_color = W.info_get("nick_color_name", room.topic_author)
            author = room.topic_author

            if author in room.users:
                user = room.users[author]
                nick_color = user.nick_color
                author = user.display_name

            author = ("{nick_color}{user}{ncolor}").format(
                nick_color=W.color(nick_color),
                user=author,
                ncolor=W.color("reset"))

            # TODO print old topic if configured so
            # TODO nick display name if configured so and found
            message = ("{prefix}{nick} has changed "
                       "the topic for {chan_color}{room}{ncolor} "
                       "to \"{topic}\"").format(
                           prefix=W.prefix("network"),
                           nick=author,
                           chan_color=W.color("chat_channel"),
                           ncolor=W.color("reset"),
                           room=strip_matrix_server(room.alias),
                           topic=topic)

            tags = "matrix_topic,no_highlight,log3,matrix_id_{event_id}".format(
                event_id=event['event_id'])

            date = date_from_age(topic_age)

            W.prnt_date_tags(buf, date, tags, message)

        elif event['type'] == "m.room.redaction":
            matrix_handle_room_redaction(server, room_id, event)

        elif event["type"] == "m.room.power_levels":
            matrix_handle_room_power_levels(server, room_id, event)

        # These events are unimportant for us.
        elif event["type"] in ["m.room.create", "m.room.join_rules",
                               "m.room.history_visibility",
                               "m.room.canonical_alias",
                               "m.room.guest_access",
                               "m.room.third_party_invite"]:
            pass

        elif event["type"] == "m.room.name":
            buf = server.buffers[room_id]
            room = server.rooms[room_id]

            name = event['content']['name']

            if not name:
                return

            room.alias = name
            W.buffer_set(buf, "name", name)
            W.buffer_set(buf, "short_name", name)
            W.buffer_set(buf, "localvar_set_channel", name)

        elif event["type"] == "m.room.encryption":
            buf = server.buffers[room_id]
            room = server.rooms[room_id]
            room.encrypted = True
            message = ("{prefix}This room is encrypted, encryption is "
                       "currently unsuported. Message sending is disabled for "
                       "this room.").format(prefix=W.prefix("error"))
            W.prnt(buf, message)

        # TODO implement message decryption
        elif event["type"] == "m.room.encrypted":
            pass

        else:
            message = ("{prefix}Handling of room event type "
                       "{type} not implemented").format(
                           type=event['type'],
                           prefix=W.prefix("error"))
            W.prnt(server.server_buffer, message)


def matrix_handle_invite_events(server, room_id, events):
    # type: (MatrixServer, str, List[Dict[str, Any]]) -> None
    for event in events:
        if event["type"] != "m.room.member":
            continue

        if 'membership' not in event:
            continue

        if event["membership"] == "invite":
            sender = event["sender"]
            # TODO does this go to the server buffer or to the channel buffer?
            message = ("{prefix}You have been invited to {chan_color}{channel}"
                       "{ncolor} by {nick_color}{nick}{ncolor}").format(
                           prefix=W.prefix("network"),
                           chan_color=W.color("chat_channel"),
                           channel=room_id,
                           ncolor=W.color("reset"),
                           nick_color=W.color("chat_nick"),
                           nick=sender)
            W.prnt(server.server_buffer, message)


def matrix_handle_room_info(server, room_info):
    # type: (MatrixServer, Dict) -> None
    for room_id, room in room_info['join'].items():
        if not room_id:
            continue

        if room_id not in server.buffers:
            matrix_create_room_buffer(server, room_id)

        if not server.rooms[room_id].prev_batch:
            server.rooms[room_id].prev_batch = room['timeline']['prev_batch']

        matrix_handle_room_events(server, room_id, room['state']['events'])
        matrix_handle_room_events(server, room_id, room['timeline']['events'])

    for room_id, room in room_info['invite'].items():
        matrix_handle_invite_events(
            server,
            room_id,
            room['invite_state']['events']
        )


def matrix_sort_old_messages(server, room_id):
    lines = []
    buf = server.buffers[room_id]

    own_lines = W.hdata_pointer(W.hdata_get('buffer'), buf, 'own_lines')

    if own_lines:
        hdata_line = W.hdata_get('line')
        hdata_line_data = W.hdata_get('line_data')
        line = W.hdata_pointer(
            W.hdata_get('lines'),
            own_lines,
            'first_line'
        )

        while line:
            data = W.hdata_pointer(hdata_line, line, 'data')

            line_data = {}

            if data:
                date = W.hdata_time(hdata_line_data, data, 'date')
                print_date = W.hdata_time(hdata_line_data, data,
                                          'date_printed')
                tags = tags_from_line_data(data)
                prefix = W.hdata_string(hdata_line_data, data, 'prefix')
                message = W.hdata_string(hdata_line_data, data,
                                         'message')

                line_data = {'date': date,
                             'date_printed': print_date,
                             'tags_array': ','.join(tags),
                             'prefix': prefix,
                             'message': message}

                lines.append(line_data)

            line = W.hdata_move(hdata_line, line, 1)

        sorted_lines = sorted(lines, key=itemgetter('date'))
        lines = []

        # We need to convert the dates to a string for hdata_update(), this
        # will reverse the list at the same time
        while sorted_lines:
            line = sorted_lines.pop()
            new_line = {k: str(v) for k, v in line.items()}
            lines.append(new_line)

        matrix_update_buffer_lines(lines, own_lines)


def matrix_update_buffer_lines(new_lines, own_lines):
    hdata_line = W.hdata_get('line')
    hdata_line_data = W.hdata_get('line_data')

    line = W.hdata_pointer(
        W.hdata_get('lines'),
        own_lines,
        'first_line'
    )

    while line:
        data = W.hdata_pointer(hdata_line, line, 'data')

        if data:
            W.hdata_update(hdata_line_data, data, new_lines.pop())

        line = W.hdata_move(hdata_line, line, 1)


def matrix_handle_old_messages(server, room_id, events):
    for event in events:
        if event['type'] == 'm.room.message':
            matrix_handle_room_messages(server, room_id, event, old=True)
        # TODO do we wan't to handle topics joins/quits here?
        else:
            pass

    matrix_sort_old_messages(server, room_id)


def matrix_handle_message(
        server,        # type: MatrixServer
        message_type,  # type: MessageType
        response,      # type: Dict[str, Any]
        extra_data     # type: Dict[str, Any]
):
    # type: (...) -> None

    if message_type is MessageType.LOGIN:
        server.access_token = response["access_token"]
        server.user_id = response["user_id"]
        message = MatrixMessage(server, GLOBAL_OPTIONS, MessageType.SYNC)
        send_or_queue(server, message)

    elif message_type is MessageType.SYNC:
        next_batch = response['next_batch']

        # we got the same batch again, nothing to do
        if next_batch == server.next_batch:
            matrix_sync(server)
            return

        room_info = response['rooms']
        matrix_handle_room_info(server, room_info)

        server.next_batch = next_batch

        # TODO add a delay to this
        matrix_sync(server)

    elif message_type is MessageType.SEND:
        author   = extra_data["author"]
        message  = extra_data["message"]
        room_id  = extra_data["room_id"]
        date     = int(time.time())
        # TODO the event_id can be missing if sending has failed for
        # some reason
        event_id = response["event_id"]

        # This message will be part of the next sync, we already printed it out
        # so ignore it in the sync.
        server.ignore_event_list.append(event_id)

        tag = ("notify_none,no_highlight,self_msg,log1,nick_{a},"
               "prefix_nick_{color},matrix_id_{event_id},"
               "matrix_message").format(
                   a=author,
                   color=color_for_tags("weechat.color.chat_nick_self"),
                   event_id=event_id)

        data = "{author}\t{msg}".format(author=author, msg=message)

        buf = server.buffers[room_id]
        W.prnt_date_tags(buf, date, tag, data)

    elif message_type == MessageType.ROOM_MSG:
        # Response has no messages, that is we already got the oldest message
        # in a previous request, nothing to do
        if not response['chunk']:
            return

        room_id = response['chunk'][0]['room_id']
        room = server.rooms[room_id]

        matrix_handle_old_messages(server, room_id, response['chunk'])

        room.prev_batch = response['end']

    # Nothing to do here, we'll handle state changes and redactions in the sync
    elif (message_type == MessageType.STATE or
          message_type == MessageType.REDACT):
        pass

    else:
        server_buffer_prnt(
            server,
            "Handling of message type {type} not implemented".format(
                type=message_type))


def matrix_sync(server):
    message = MatrixMessage(server, GLOBAL_OPTIONS, MessageType.SYNC)
    server.send_queue.append(message)


def matrix_login(server):
    # type: (MatrixServer) -> None
    post_data = {"type": "m.login.password",
                 "user": server.user,
                 "password": server.password,
                 "initial_device_display_name": server.device_name}

    message = MatrixMessage(
        server,
        GLOBAL_OPTIONS,
        MessageType.LOGIN,
        data=post_data
    )
    send_or_queue(server, message)


@utf8_decode
def receive_cb(server_name, file_descriptor):
    server = SERVERS[server_name]

    while True:
        try:
            data = server.socket.recv(4096)
        except ssl.SSLWantReadError:
            break
        except socket.error as error:
            disconnect(server)

            # Queue the failed message for resending
            if server.receive_queue:
                message = server.receive_queue.popleft()
                server.send_queue.appendleft(message)

            server_buffer_prnt(server, pprint.pformat(error))
            return W.WEECHAT_RC_OK

        if not data:
            server_buffer_prnt(server, "No data while reading")

            # Queue the failed message for resending
            if server.receive_queue:
                message = server.receive_queue.popleft()
                server.send_queue.appendleft(message)

            disconnect(server)
            break

        received = len(data)  # type: int
        parsed_bytes = server.http_parser.execute(data, received)

        assert parsed_bytes == received

        if server.http_parser.is_partial_body():
            server.http_buffer.append(server.http_parser.recv_body())

        if server.http_parser.is_message_complete():
            status = server.http_parser.get_status_code()
            headers = server.http_parser.get_headers()
            body = b"".join(server.http_buffer)

            message = server.receive_queue.popleft()
            message.response = HttpResponse(status, headers, body)
            receive_time = time.time()
            message.receive_time = receive_time

            prnt_debug(DebugType.MESSAGING, server,
                       ("{prefix}Received message of type {t} and "
                        "status {s}").format(
                            prefix=W.prefix("error"),
                            t=message.type,
                            s=status))

            # Message done, reset the parser state.
            server.reset_parser()

            handle_http_response(server, message)
            break

    return W.WEECHAT_RC_OK


def close_socket(server):
    # type: (MatrixServer) -> None
    server.socket.shutdown(socket.SHUT_RDWR)
    server.socket.close()


def server_buffer_set_title(server):
    # type: (MatrixServer) -> None
    if server.numeric_address:
        ip_string = " ({address})".format(address=server.numeric_address)
    else:
        ip_string = ""

    title = ("Matrix: {address}/{port}{ip}").format(
        address=server.address,
        port=server.port,
        ip=ip_string)

    W.buffer_set(server.server_buffer, "title", title)


def create_server_buffer(server):
    # type: (MatrixServer) -> None
    server.server_buffer = W.buffer_new(
        server.name,
        "server_buffer_cb",
        server.name,
        "",
        ""
    )

    server_buffer_set_title(server)
    W.buffer_set(server.server_buffer, "localvar_set_type", 'server')
    W.buffer_set(server.server_buffer, "localvar_set_nick", server.user)
    W.buffer_set(server.server_buffer, "localvar_set_server", server.name)
    W.buffer_set(server.server_buffer, "localvar_set_channel", server.name)

    # TODO merge without core
    if GLOBAL_OPTIONS.look_server_buf == ServerBufferType.MERGE_CORE:
        W.buffer_merge(server.server_buffer, W.buffer_search_main())
    elif GLOBAL_OPTIONS.look_server_buf == ServerBufferType.MERGE:
        pass
    else:
        pass


@utf8_decode
def connect_cb(data, status, gnutls_rc, sock, error, ip_address):
    # pylint: disable=too-many-arguments,too-many-branches
    status_value = int(status)  # type: int
    server = SERVERS[data]

    if status_value == W.WEECHAT_HOOK_CONNECT_OK:
        file_descriptor = int(sock)  # type: int
        sock = wrap_socket(server, file_descriptor)

        if sock:
            server.socket = sock
            hook = W.hook_fd(
                server.socket.fileno(),
                1, 0, 0,
                "receive_cb",
                server.name
            )

            server.fd_hook         = hook
            server.connected       = True
            server.connecting      = False
            server.reconnect_count = 0
            server.numeric_address = ip_address

            server_buffer_set_title(server)
            server_buffer_prnt(server, "Connected")

            if not server.access_token:
                matrix_login(server)
        else:
            reconnect(server)
        return W.WEECHAT_RC_OK

    elif status_value == W.WEECHAT_HOOK_CONNECT_ADDRESS_NOT_FOUND:
        W.prnt(
            server.server_buffer,
            '{address} not found'.format(address=ip_address)
        )

    elif status_value == W.WEECHAT_HOOK_CONNECT_IP_ADDRESS_NOT_FOUND:
        W.prnt(server.server_buffer, 'IP address not found')

    elif status_value == W.WEECHAT_HOOK_CONNECT_CONNECTION_REFUSED:
        W.prnt(server.server_buffer, 'Connection refused')

    elif status_value == W.WEECHAT_HOOK_CONNECT_PROXY_ERROR:
        W.prnt(
            server.server_buffer,
            'Proxy fails to establish connection to server'
        )

    elif status_value == W.WEECHAT_HOOK_CONNECT_LOCAL_HOSTNAME_ERROR:
        W.prnt(server.server_buffer, 'Unable to set local hostname')

    elif status_value == W.WEECHAT_HOOK_CONNECT_GNUTLS_INIT_ERROR:
        W.prnt(server.server_buffer, 'TLS init error')

    elif status_value == W.WEECHAT_HOOK_CONNECT_GNUTLS_HANDSHAKE_ERROR:
        W.prnt(server.server_buffer, 'TLS Handshake failed')

    elif status_value == W.WEECHAT_HOOK_CONNECT_MEMORY_ERROR:
        W.prnt(server.server_buffer, 'Not enough memory')

    elif status_value == W.WEECHAT_HOOK_CONNECT_TIMEOUT:
        W.prnt(server.server_buffer, 'Timeout')

    elif status_value == W.WEECHAT_HOOK_CONNECT_SOCKET_ERROR:
        W.prnt(server.server_buffer, 'Unable to create socket')
    else:
        W.prnt(
            server.server_buffer,
            'Unexpected error: {status}'.format(status=status_value)
        )

    reconnect(server)
    return W.WEECHAT_RC_OK


def reconnect(server):
    # type: (MatrixServer) -> None
    server.connecting = True
    timeout = server.reconnect_count * 5 * 1000

    if timeout > 0:
        server_buffer_prnt(
            server,
            "Reconnecting in {timeout} seconds.".format(
                timeout=timeout / 1000))
        W.hook_timer(timeout, 0, 1, "reconnect_cb", server.name)
    else:
        connect(server)

    server.reconnect_count += 1


@utf8_decode
def reconnect_cb(server_name, remaining):
    server = SERVERS[server_name]
    connect(server)

    return W.WEECHAT_RC_OK


def connect(server):
    # type: (MatrixServer) -> int
    if not server.address or not server.port:
        message = "{prefix}Server address or port not set".format(
            prefix=W.prefix("error"))
        W.prnt("", message)
        return False

    if not server.user or not server.password:
        message = "{prefix}User or password not set".format(
            prefix=W.prefix("error"))
        W.prnt("", message)
        return False

    if server.connected:
        return True

    if not server.server_buffer:
        create_server_buffer(server)

    if not server.timer_hook:
        server.timer_hook = W.hook_timer(
            1 * 1000,
            0,
            0,
            "matrix_timer_cb",
            server.name
        )

    W.hook_connect("", server.address, server.port, 1, 0, "",
                   "connect_cb", server.name)

    return W.WEECHAT_RC_OK


@utf8_decode
def room_input_cb(server_name, buffer, input_data):
    server = SERVERS[server_name]

    if not server.connected:
        message = "{prefix}you are not connected to the server".format(
            prefix=W.prefix("error"))
        W.prnt(buffer, message)
        return W.WEECHAT_RC_ERROR

    room_id = key_from_value(server.buffers, buffer)
    room = server.rooms[room_id]

    if room.encrypted:
        return W.WEECHAT_RC_OK

    formatted_data = colors.parse_input_line(input_data)

    body = {
        "msgtype": "m.text",
        "body": colors.formatted_to_plain(formatted_data)
    }

    if colors.formatted(formatted_data):
        body["format"] = "org.matrix.custom.html"
        body["formatted_body"] = colors.formatted_to_html(formatted_data)

    extra_data = {
        "author": server.user,
        "message": colors.formatted_to_weechat(W, formatted_data),
        "room_id": room_id
    }

    message = MatrixMessage(server, GLOBAL_OPTIONS, MessageType.SEND,
                            data=body, room_id=room_id,
                            extra_data=extra_data)

    send_or_queue(server, message)
    return W.WEECHAT_RC_OK


@utf8_decode
def room_close_cb(data, buffer):
    W.prnt("", "Buffer '%s' will be closed!" %
           W.buffer_get_string(buffer, "name"))
    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_timer_cb(server_name, remaining_calls):
    server = SERVERS[server_name]

    if not server.connected:
        if not server.connecting:
            server_buffer_prnt(server, "Reconnecting timeout blaaaa")
            reconnect(server)
        return W.WEECHAT_RC_OK

    while server.send_queue:
        message = server.send_queue.popleft()
        prnt_debug(DebugType.MESSAGING, server,
                   ("Timer hook found message of type {t} in queue. Sending "
                    "out.".format(t=message.type)))

        if not send(server, message):
            # We got an error while sending the last message return the message
            # to the queue and exit the loop
            server.send_queue.appendleft(message)
            break

    for message in server.message_queue:
        server_buffer_prnt(
            server,
            "Handling message: {message}".format(message=message))

    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_config_reload_cb(data, config_file):
    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_config_server_read_cb(
        data, config_file, section,
        option_name, value
):

    return_code = W.WEECHAT_CONFIG_OPTION_SET_ERROR

    if option_name:
        server_name, option = option_name.rsplit('.', 1)
        server = None

        if server_name in SERVERS:
            server = SERVERS[server_name]
        else:
            server = MatrixServer(server_name, W, config_file)
            SERVERS[server.name] = server

        # Ignore invalid options
        if option in server.options:
            return_code = W.config_option_set(server.options[option], value, 1)

    # TODO print out error message in case of erroneous return_code

    return return_code


@utf8_decode
def matrix_config_server_write_cb(data, config_file, section_name):
    if not W.config_write_line(config_file, section_name, ""):
        return W.WECHAT_CONFIG_WRITE_ERROR

    for server in SERVERS.values():
        for option in server.options.values():
            if not W.config_write_option(config_file, option):
                return W.WECHAT_CONFIG_WRITE_ERROR

    return W.WEECHAT_CONFIG_WRITE_OK


@utf8_decode
def matrix_config_change_cb(data, option):
    option_name = key_from_value(GLOBAL_OPTIONS.options, option)

    if option_name == "redactions":
        GLOBAL_OPTIONS.redaction_type = RedactType(W.config_integer(option))
    elif option_name == "server_buffer":
        GLOBAL_OPTIONS.look_server_buf = ServerBufferType(
            W.config_integer(option))
    elif option_name == "max_initial_sync_events":
        GLOBAL_OPTIONS.sync_limit = W.config_integer(option)
    elif option_name == "max_backlog_sync_events":
        GLOBAL_OPTIONS.backlog_limit = W.config_integer(option)
    elif option_name == "fetch_backlog_on_pgup":
        GLOBAL_OPTIONS.enable_backlog = W.config_boolean(option)

        if GLOBAL_OPTIONS.enable_backlog:
            if not GLOBAL_OPTIONS.page_up_hook:
                hook_page_up(CONFIG)
        else:
            if GLOBAL_OPTIONS.page_up_hook:
                W.unhook(GLOBAL_OPTIONS.page_up_hook)
                GLOBAL_OPTIONS.page_up_hook = None

    return 1


def read_matrix_config():
    # type: () -> bool
    return_code = W.config_read(CONFIG)
    if return_code == W.WEECHAT_CONFIG_READ_OK:
        return True
    elif return_code == W.WEECHAT_CONFIG_READ_MEMORY_ERROR:
        return False
    elif return_code == W.WEECHAT_CONFIG_READ_FILE_NOT_FOUND:
        return True
    return False


@utf8_decode
def matrix_unload_cb():
    for section in ["network", "look", "color", "server"]:
        section_pointer = W.config_search_section(CONFIG, section)
        W.config_section_free_options(section_pointer)
        W.config_section_free(section_pointer)

    W.config_free(CONFIG)

    return W.WEECHAT_RC_OK


def check_server_existence(server_name, servers):
    if server_name not in servers:
        message = "{prefix}matrix: No such server: {server} found".format(
            prefix=W.prefix("error"), server=server_name)
        W.prnt("", message)
        return False
    return True


def matrix_command_debug(args):
    if not args:
        message = ("{prefix}matrix: Too few arguments for command "
                   "\"/matrix debug\" (see the help for the command: "
                   "/matrix help debug").format(prefix=W.prefix("error"))
        W.prnt("", message)
        return

    def toggle_debug(debug_type):
        if debug_type in GLOBAL_OPTIONS.debug:
            message = ("{prefix}matrix: Disabling matrix {t} "
                       "debugging.").format(
                           prefix=W.prefix("error"),
                           t=debug_type)
            W.prnt("", message)
            GLOBAL_OPTIONS.debug.remove(debug_type)
        else:
            message = ("{prefix}matrix: Enabling matrix {t} "
                       "debugging.").format(
                           prefix=W.prefix("error"),
                           t=debug_type)
            W.prnt("", message)
            GLOBAL_OPTIONS.debug.append(debug_type)

    for command in args:
        if command == "network":
            toggle_debug(DebugType.NETWORK)
        elif command == "messaging":
            toggle_debug(DebugType.MESSAGING)
        elif command == "timing":
            toggle_debug(DebugType.TIMING)
        else:
            message = ("{prefix}matrix: Unknown matrix debug "
                       "type \"{t}\".").format(
                           prefix=W.prefix("error"),
                           t=command)
            W.prnt("", message)


def matrix_command_help(args):
    if not args:
        message = ("{prefix}matrix: Too few arguments for command "
                   "\"/matrix help\" (see the help for the command: "
                   "/matrix help help").format(prefix=W.prefix("error"))
        W.prnt("", message)
        return

    for command in args:
        message = ""

        if command == "connect":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/connect{ncolor} "
                       "<server-name> [<server-name>...]"
                       "\n\n"
                       "connect to Matrix server(s)"
                       "\n\n"
                       "server-name: server to connect to"
                       "(internal name)").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        elif command == "disconnect":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/disconnect{ncolor} "
                       "<server-name> [<server-name>...]"
                       "\n\n"
                       "disconnect from Matrix server(s)"
                       "\n\n"
                       "server-name: server to disconnect"
                       "(internal name)").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        elif command == "reconnect":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/reconnect{ncolor} "
                       "<server-name> [<server-name>...]"
                       "\n\n"
                       "reconnect to Matrix server(s)"
                       "\n\n"
                       "server-name: server to reconnect"
                       "(internal name)").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        elif command == "server":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/server{ncolor} "
                       "add <server-name> <hostname>[:<port>]"
                       "\n                  "
                       "delete|list|listfull <server-name>"
                       "\n\n"
                       "list, add, or remove Matrix servers"
                       "\n\n"
                       "       list: list servers (without argument, this "
                       "list is displayed)\n"
                       "   listfull: list servers with detailed info for each "
                       "server\n"
                       "        add: add a new server\n"
                       "     delete: delete a server\n"
                       "server-name: server to reconnect (internal name)\n"
                       "   hostname: name or IP address of server\n"
                       "       port: port of server (default: 8448)\n"
                       "\n"
                       "Examples:"
                       "\n  /server listfull"
                       "\n  /server add matrix matrix.org:80"
                       "\n  /server del matrix").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        elif command == "help":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/help{ncolor} "
                       "<matrix-command> [<matrix-command>...]"
                       "\n\n"
                       "display help about Matrix commands"
                       "\n\n"
                       "matrix-command: a Matrix command name"
                       "(internal name)").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        elif command == "debug":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/debug{ncolor} "
                       "<debug-type> [<debug-type>...]"
                       "\n\n"
                       "enable/disable degugging for a Matrix subsystem"
                       "\n\n"
                       "debug-type: a Matrix debug type, one of messaging, "
                       "timing, network").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        else:
            message = ("{prefix}matrix: No help available, \"{command}\" "
                       "is not a matrix command").format(
                           prefix=W.prefix("error"),
                           command=command)

        W.prnt("", "")
        W.prnt("", message)

        return


def matrix_server_command_listfull(args):
    def get_value_string(value, default_value):
        if value == default_value:
            if not value:
                value = "''"
            value_string = "  ({value})".format(value=value)
        else:
            value_string = "{color}{value}{ncolor}".format(
                color=W.color("chat_value"),
                value=value,
                ncolor=W.color("reset"))

        return value_string

    for server_name in args:
        if server_name not in SERVERS:
            continue

        server = SERVERS[server_name]
        connected = ""

        W.prnt("", "")

        if server.connected:
            connected = "connected"
        else:
            connected = "not connected"

        message = ("Server: {server_color}{server}{delimiter_color}"
                   " [{ncolor}{connected}{delimiter_color}]"
                   "{ncolor}").format(
                       server_color=W.color("chat_server"),
                       server=server.name,
                       delimiter_color=W.color("chat_delimiters"),
                       connected=connected,
                       ncolor=W.color("reset"))

        W.prnt("", message)

        option = server.options["autoconnect"]
        default_value = W.config_string_default(option)
        value = W.config_string(option)

        value_string = get_value_string(value, default_value)
        message = "  autoconnect. : {value}".format(value=value_string)

        W.prnt("", message)

        option = server.options["address"]
        default_value = W.config_string_default(option)
        value = W.config_string(option)

        value_string = get_value_string(value, default_value)
        message = "  address. . . : {value}".format(value=value_string)

        W.prnt("", message)

        option = server.options["port"]
        default_value = str(W.config_integer_default(option))
        value = str(W.config_integer(option))

        value_string = get_value_string(value, default_value)
        message = "  port . . . . : {value}".format(value=value_string)

        W.prnt("", message)

        option = server.options["username"]
        default_value = W.config_string_default(option)
        value = W.config_string(option)

        value_string = get_value_string(value, default_value)
        message = "  username . . : {value}".format(value=value_string)

        W.prnt("", message)

        option = server.options["password"]
        value = W.config_string(option)

        if value:
            value = "(hidden)"

        value_string = get_value_string(value, '')
        message = "  password . . : {value}".format(value=value_string)

        W.prnt("", message)


def matrix_server_command_delete(args):
    for server_name in args:
        if check_server_existence(server_name, SERVERS):
            server = SERVERS[server_name]

            if server.connected:
                message = ("{prefix}matrix: you can not delete server "
                           "{color}{server}{ncolor} because you are "
                           "connected to it. Try \"/matrix disconnect "
                           "{color}{server}{ncolor}\" before.").format(
                               prefix=W.prefix("error"),
                               color=W.color("chat_server"),
                               ncolor=W.color("reset"),
                               server=server.name)
                W.prnt("", message)
                return

            for buf in server.buffers.values():
                W.buffer_close(buf)

            if server.server_buffer:
                W.buffer_close(server.server_buffer)

            for option in server.options.values():
                W.config_option_free(option)

            message = ("matrix: server {color}{server}{ncolor} has been "
                       "deleted").format(
                           server=server.name,
                           color=W.color("chat_server"),
                           ncolor=W.color("reset"))

            del SERVERS[server.name]
            server = None

            W.prnt("", message)


def matrix_server_command_add(args):
    if len(args) < 2:
        message = ("{prefix}matrix: Too few arguments for command "
                   "\"/matrix server add\" (see the help for the command: "
                   "/matrix help server").format(prefix=W.prefix("error"))
        W.prnt("", message)
        return
    elif len(args) > 4:
        message = ("{prefix}matrix: Too many arguments for command "
                   "\"/matrix server add\" (see the help for the command: "
                   "/matrix help server").format(prefix=W.prefix("error"))
        W.prnt("", message)
        return

    def remove_server(server):
        for option in server.options.values():
            W.config_option_free(option)
        del SERVERS[server.name]

    server_name = args[0]

    if server_name in SERVERS:
        message = ("{prefix}matrix: server {color}{server}{ncolor} "
                   "already exists, can't add it").format(
                       prefix=W.prefix("error"),
                       color=W.color("chat_server"),
                       server=server_name,
                       ncolor=W.color("reset"))
        W.prnt("", message)
        return

    server = MatrixServer(args[0], W, CONFIG)
    SERVERS[server.name] = server

    if len(args) >= 2:
        try:
            host, port = args[1].split(":", 1)
        except ValueError:
            host, port = args[1], None

        return_code = W.config_option_set(
            server.options["address"],
            host,
            1
        )

        if return_code == W.WEECHAT_CONFIG_OPTION_SET_ERROR:
            remove_server(server)
            message = ("{prefix}Failed to set address for server "
                       "{color}{server}{ncolor}, failed to add "
                       "server.").format(
                           prefix=W.prefix("error"),
                           color=W.color("chat_server"),
                           server=server.name,
                           ncolor=W.color("reset"))

            W.prnt("", message)
            server = None
            return

        if port:
            return_code = W.config_option_set(
                server.options["port"],
                port,
                1
            )
            if return_code == W.WEECHAT_CONFIG_OPTION_SET_ERROR:
                remove_server(server)
                message = ("{prefix}Failed to set port for server "
                           "{color}{server}{ncolor}, failed to add "
                           "server.").format(
                               prefix=W.prefix("error"),
                               color=W.color("chat_server"),
                               server=server.name,
                               ncolor=W.color("reset"))

                W.prnt("", message)
                server = None
                return

    if len(args) >= 3:
        user = args[2]
        return_code = W.config_option_set(
            server.options["username"],
            user,
            1
        )

        if return_code == W.WEECHAT_CONFIG_OPTION_SET_ERROR:
            remove_server(server)
            message = ("{prefix}Failed to set user for server "
                       "{color}{server}{ncolor}, failed to add "
                       "server.").format(
                           prefix=W.prefix("error"),
                           color=W.color("chat_server"),
                           server=server.name,
                           ncolor=W.color("reset"))

            W.prnt("", message)
            server = None
            return

    if len(args) == 4:
        password = args[3]

        return_code = W.config_option_set(
            server.options["password"],
            password,
            1
        )
        if return_code == W.WEECHAT_CONFIG_OPTION_SET_ERROR:
            remove_server(server)
            message = ("{prefix}Failed to set password for server "
                       "{color}{server}{ncolor}, failed to add "
                       "server.").format(
                           prefix=W.prefix("error"),
                           color=W.color("chat_server"),
                           server=server.name,
                           ncolor=W.color("reset"))
            W.prnt("", message)
            server = None
            return

    message = ("matrix: server {color}{server}{ncolor} "
               "has been added").format(
                   server=server.name,
                   color=W.color("chat_server"),
                   ncolor=W.color("reset"))
    W.prnt("", message)


def matrix_server_command(command, args):
    def list_servers(_):
        if SERVERS:
            W.prnt("", "\nAll matrix servers:")
            for server in SERVERS:
                W.prnt("", "    {color}{server}".format(
                    color=W.color("chat_server"),
                    server=server
                ))

    # TODO the argument for list and listfull is used as a match word to
    # find/filter servers, we're currently match exactly to the whole name
    if command == 'list':
        list_servers(args)
    elif command == 'listfull':
        matrix_server_command_listfull(args)
    elif command == 'add':
        matrix_server_command_add(args)
    elif command == 'delete':
        matrix_server_command_delete(args)
    else:
        message = ("{prefix}matrix: Error: unknown matrix server command, "
                   "\"{command}\" (type /matrix help server for help)").format(
                       prefix=W.prefix("error"),
                       command=command)
        W.prnt("", message)


@utf8_decode
def matrix_command_cb(data, buffer, args):
    def connect_server(args):
        for server_name in args:
            if check_server_existence(server_name, SERVERS):
                server = SERVERS[server_name]
                connect(server)

    def disconnect_server(args):
        for server_name in args:
            if check_server_existence(server_name, SERVERS):
                server = SERVERS[server_name]
                if server.connected:
                    W.unhook(server.timer_hook)
                    server.timer_hook = None
                    server.access_token = ""
                    disconnect(server)

    split_args = list(filter(bool, args.split(' ')))

    if len(split_args) < 1:
        message = ("{prefix}matrix: Too few arguments for command "
                   "\"/matrix\" (see the help for the command: "
                   "/help matrix").format(prefix=W.prefix("error"))
        W.prnt("", message)
        return W.WEECHAT_RC_ERROR

    command, args = split_args[0], split_args[1:]

    if command == 'connect':
        connect_server(args)

    elif command == 'disconnect':
        disconnect_server(args)

    elif command == 'reconnect':
        disconnect_server(args)
        connect_server(args)

    elif command == 'server':
        if len(args) >= 1:
            subcommand, args = args[0], args[1:]
            matrix_server_command(subcommand, args)
        else:
            matrix_server_command("list", "")

    elif command == 'help':
        matrix_command_help(args)

    elif command == 'debug':
        matrix_command_debug(args)

    else:
        message = ("{prefix}matrix: Error: unknown matrix command, "
                   "\"{command}\" (type /help matrix for help)").format(
                       prefix=W.prefix("error"),
                       command=command)
        W.prnt("", message)

    return W.WEECHAT_RC_OK


def add_servers_to_completion(completion):
    for server_name in SERVERS:
        W.hook_completion_list_add(
            completion,
            server_name,
            0,
            W.WEECHAT_LIST_POS_SORT
        )


@utf8_decode
def server_command_completion_cb(data, completion_item, buffer, completion):
    buffer_input = W.buffer_get_string(buffer, "input").split()

    args = buffer_input[1:]
    commands = ['add', 'delete', 'list', 'listfull']

    def complete_commands():
        for command in commands:
            W.hook_completion_list_add(
                completion,
                command,
                0,
                W.WEECHAT_LIST_POS_SORT
            )

    if len(args) == 1:
        complete_commands()

    elif len(args) == 2:
        if args[1] not in commands:
            complete_commands()
        else:
            if args[1] == 'delete' or args[1] == 'listfull':
                add_servers_to_completion(completion)

    elif len(args) == 3:
        if args[1] == 'delete' or args[1] == 'listfull':
            if args[2] not in SERVERS:
                add_servers_to_completion(completion)

    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_server_completion_cb(data, completion_item, buffer, completion):
    add_servers_to_completion(completion)
    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_command_completion_cb(data, completion_item, buffer, completion):
    for command in [
            "connect",
            "disconnect",
            "reconnect",
            "server",
            "help",
            "debug"
    ]:
        W.hook_completion_list_add(
            completion,
            command,
            0,
            W.WEECHAT_LIST_POS_SORT)
    return W.WEECHAT_RC_OK


def create_default_server(config_file):
    server = MatrixServer('matrix.org', W, config_file)
    SERVERS[server.name] = server

    W.config_option_set(server.options["address"], "matrix.org", 1)

    return True


@utf8_decode
def matrix_command_topic_cb(data, buffer, command):
    for server in SERVERS.values():
        if buffer in server.buffers.values():
            topic = None
            room_id = key_from_value(server.buffers, buffer)
            split_command = command.split(' ', 1)

            if len(split_command) == 2:
                topic = split_command[1]

            if not topic:
                room = server.rooms[room_id]
                if not room.topic:
                    return W.WEECHAT_RC_OK

                message = ("{prefix}Topic for {color}{room}{ncolor} is "
                           "\"{topic}\"").format(
                               prefix=W.prefix("network"),
                               color=W.color("chat_buffer"),
                               ncolor=W.color("reset"),
                               room=room.alias,
                               topic=room.topic)

                date = int(time.time())
                topic_date = room.topic_date.strftime("%a, %d %b %Y "
                                                      "%H:%M:%S")

                tags = "matrix_topic,log1"
                W.prnt_date_tags(buffer, date, tags, message)

                # TODO the nick should be colored

                # TODO we should use the display name as well as
                # the user name here
                message = ("{prefix}Topic set by {author} on "
                           "{date}").format(
                               prefix=W.prefix("network"),
                               author=room.topic_author,
                               date=topic_date)
                W.prnt_date_tags(buffer, date, tags, message)

                return W.WEECHAT_RC_OK_EAT

            body = {"topic": topic}

            message = MatrixMessage(
                server,
                GLOBAL_OPTIONS,
                MessageType.STATE,
                data=body,
                room_id=room_id,
                extra_id="m.room.topic"
            )
            send_or_queue(server, message)

            return W.WEECHAT_RC_OK_EAT

        elif buffer == server.server_buffer:
            message = ("{prefix}matrix: command \"topic\" must be "
                       "executed on a Matrix channel buffer").format(
                           prefix=W.prefix("error"))
            W.prnt(buffer, message)
            return W.WEECHAT_RC_OK_EAT

    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_bar_item_plugin(data, item, window, buffer, extra_info):
    # pylint: disable=unused-argument
    for server in SERVERS.values():
        if (buffer in server.buffers.values() or
                buffer == server.server_buffer):
            return "matrix{color}/{color_fg}{name}".format(
                color=W.color("bar_delim"),
                color_fg=W.color("bar_fg"),
                name=server.name)

    return ""


@utf8_decode
def matrix_bar_item_name(data, item, window, buffer, extra_info):
    # pylint: disable=unused-argument
    for server in SERVERS.values():
        if buffer in server.buffers.values():
            color = ("status_name_ssl"
                     if server.ssl_context.check_hostname else
                     "status_name")

            room_id = key_from_value(server.buffers, buffer)

            room = server.rooms[room_id]

            return "{color}{name}".format(
                color=W.color(color),
                name=room.alias)

        elif buffer in server.server_buffer:
            color = ("status_name_ssl"
                     if server.ssl_context.check_hostname else
                     "status_name")

            return "{color}server{del_color}[{color}{name}{del_color}]".format(
                color=W.color(color),
                del_color=W.color("bar_delim"),
                name=server.name)

    return ""


def autoconnect(servers):
    for server in servers.values():
        if server.autoconnect:
            connect(server)


if __name__ == "__main__":
    if W.register(WEECHAT_SCRIPT_NAME,
                  WEECHAT_SCRIPT_AUTHOR,
                  WEECHAT_SCRIPT_VERSION,
                  WEECHAT_SCRIPT_LICENSE,
                  WEECHAT_SCRIPT_DESCRIPTION,
                  'matrix_unload_cb',
                  ''):

        # TODO if this fails we should abort and unload the script.
        CONFIG = matrix.globals.init_matrix_config()
        read_matrix_config()

        hook_commands()

        W.bar_item_new("(extra)buffer_plugin",  "matrix_bar_item_plugin",  "")
        W.bar_item_new("(extra)buffer_name", "matrix_bar_item_name", "")

        if not SERVERS:
            create_default_server(CONFIG)

        autoconnect(SERVERS)