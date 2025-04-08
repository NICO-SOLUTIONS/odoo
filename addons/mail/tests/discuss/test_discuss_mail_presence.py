# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json

try:
    import websocket as ws
except ImportError:
    ws = None

from odoo.tests import tagged, new_test_user
from odoo.addons.bus.tests.common import WebsocketCase
from odoo.addons.mail.tests.common import MailCommon
from odoo.addons.bus.models.bus import channel_with_db, json_dump


@tagged("post_install", "-at_install")
class TestMailPresence(WebsocketCase, MailCommon):
    def _receive_presence(self, sender, recipient):
        self._reset_bus()
        sent_from_user = isinstance(sender, self.env.registry["res.users"])
        receive_to_user = isinstance(recipient, self.env.registry["res.users"])
        if receive_to_user:
            session = self.authenticate(recipient.login, recipient.login)
            auth_cookie = f"session_id={session.sid};"
        else:
            self.authenticate(None, None)
            auth_cookie = f"{recipient._cookie_name}={recipient._format_auth_cookie()};"
        websocket = self.websocket_connect(cookie=auth_cookie, timeout=1)
        sender_bus_target = sender.partner_id if sent_from_user else sender
        self.subscribe(
            websocket,
            [f"odoo-presence-{sender_bus_target._name}_{sender_bus_target.id}"],
            self.env["bus.bus"]._bus_last_id(),
        )
        self.env["mail.presence"]._update_presence(sender)
        self.trigger_notification_dispatching([(sender_bus_target, "presence")])
        notifications = json.loads(websocket.recv())
        self._close_websockets()
        bus_record = self.env["bus.bus"].search([("id", "=", int(notifications[0]["id"]))])
        self.assertEqual(
            bus_record.channel,
            json_dump(channel_with_db(self.env.cr.dbname, (sender_bus_target, "presence"))),
        )
        self.assertEqual(notifications[0]["message"]["type"], "bus.bus/im_status_updated")
        self.assertEqual(notifications[0]["message"]["payload"]["im_status"], "online")
        self.assertEqual(
            notifications[0]["message"]["payload"]["partner_id" if sent_from_user else "guest_id"],
            sender_bus_target.id,
        )

    def test_receive_presences_as_guest(self):
        guest = self.env["mail.guest"].create({"name": "Guest"})
        bob = new_test_user(self.env, login="bob_user", groups="base.group_user")
        # Guest should not receive users's presence: no common channel.
        with self.assertRaises(ws._exceptions.WebSocketTimeoutException):
            self._receive_presence(sender=bob, recipient=guest)
        channel = self.env["discuss.channel"]._create_channel(group_id=None, name="General")
        channel._add_members(guests=guest, users=bob)
        # Now that they share a channel, guest should receive users's presence.
        self._receive_presence(sender=bob, recipient=guest)

        other_guest = self.env["mail.guest"].create({"name": "OtherGuest"})
        # Guest should not receive guest's presence: no common channel.
        with self.assertRaises(ws._exceptions.WebSocketTimeoutException):
            self._receive_presence(sender=other_guest, recipient=guest)
        channel._add_members(guests=other_guest)
        # Now that they share a channel, guest should receive guest's presence.
        self._receive_presence(sender=other_guest, recipient=guest)
        self.assertEqual(other_guest.im_status, "online")

    def test_receive_presences_as_portal(self):
        portal = new_test_user(self.env, login="portal_user", groups="base.group_portal")
        bob = new_test_user(self.env, login="bob_user", groups="base.group_user")
        # Portal should not receive users's presence: no common channel.
        with self.assertRaises(ws._exceptions.WebSocketTimeoutException):
            self._receive_presence(sender=bob, recipient=portal)
        channel = self.env["discuss.channel"]._create_channel(group_id=None, name="General")
        channel._add_members(users=portal | bob)
        # Now that they share a channel, portal should receive users's presence.
        self._receive_presence(sender=bob, recipient=portal)

        guest = self.env["mail.guest"].create({"name": "Guest"})
        # Portal should not receive guest's presence: no common channel.
        with self.assertRaises(ws._exceptions.WebSocketTimeoutException):
            self._receive_presence(sender=guest, recipient=portal)
        channel._add_members(guests=guest)
        # Now that they share a channel, portal should receive guest's presence.
        self._receive_presence(sender=guest, recipient=portal)
        self.assertEqual(guest.im_status, "online")

    def test_receive_presences_as_internal(self):
        internal = new_test_user(self.env, login="internal_user", groups="base.group_user")
        guest = self.env["mail.guest"].create({"name": "Guest"})
        # Internal can access guest's presence regardless of their channels.
        self._receive_presence(sender=guest, recipient=internal)
        # Internal can access users's presence regardless of their channels.
        bob = new_test_user(self.env, login="bob_user", groups="base.group_user")
        self._receive_presence(sender=bob, recipient=internal)
        self.assertEqual(bob.im_status, "online")
