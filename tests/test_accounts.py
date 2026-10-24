"""Accounts and audit log. Run: python -m unittest discover tests

Standard library only, and each test uses its own throwaway database, so
these run anywhere without the pipeline's dependencies or any secrets.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sermon_shorts.core import audit, users
from sermon_shorts.core.db import connect
from sermon_shorts.core.users import AccountError


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")
        self.root, self.root_token = users.init_root(self.conn, "pshouse", "Patrick S.", "sprite")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def join(self, role, login, by=None):
        _, code = users.invite(self.conn, by or self.root, role)
        return users.accept_invite(self.conn, code, login, login.title())[0]

    # --- bootstrapping ---

    def test_root_is_an_owner_with_a_working_token(self):
        self.assertEqual(self.root.role, "owner")
        self.assertTrue(self.root.is_root)
        self.assertEqual(users.authenticate(self.conn, self.root_token), self.root)

    def test_root_can_only_be_created_once(self):
        with self.assertRaises(AccountError):
            users.init_root(self.conn, "someone", "Someone", "sprite")

    # --- invites and role ladder ---

    def test_each_role_gets_exactly_its_permissions(self):
        reviewer = self.join("reviewer", "rita")
        approver = self.join("approver", "abe")
        owner = self.join("owner", "olga")
        self.assertTrue(reviewer.can("view") and reviewer.can("note"))
        self.assertFalse(reviewer.can("approve"))
        self.assertTrue(approver.can("approve") and approver.can("publish"))
        self.assertFalse(approver.can("run") or approver.can("manage_people"))
        self.assertTrue(owner.can("run") and owner.can("manage_people"))

    def test_only_owners_invite(self):
        approver = self.join("approver", "abe")
        with self.assertRaises(AccountError):
            users.invite(self.conn, approver, "reviewer")
        olga = self.join("owner", "olga")
        self.join("approver", "amy", by=olga)  # a non-root owner can invite too

    def test_invite_codes_work_once(self):
        _, code = users.invite(self.conn, self.root, "reviewer")
        users.accept_invite(self.conn, code, "rita", "Rita")
        with self.assertRaises(AccountError):
            users.accept_invite(self.conn, code, "rex", "Rex")

    def test_withdrawn_and_expired_invites_fail(self):
        invite_id, code = users.invite(self.conn, self.root, "reviewer")
        users.revoke_invite(self.conn, self.root, invite_id)
        with self.assertRaises(AccountError):
            users.accept_invite(self.conn, code, "rita", "Rita")
        _, code = users.invite(self.conn, self.root, "reviewer")
        self.conn.execute("UPDATE invites SET expires_at = '2000-01-01T00:00:00Z'")
        with self.assertRaises(AccountError):
            users.accept_invite(self.conn, code, "rita", "Rita")

    def test_logins_are_unique_even_after_removal(self):
        self.join("reviewer", "rita")
        users.remove(self.conn, self.root, "rita")
        with self.assertRaises(AccountError):
            self.join("reviewer", "rita")

    # --- changing people ---

    def test_promote_and_demote(self):
        self.join("reviewer", "rita")
        self.assertEqual(users.set_role(self.conn, self.root, "rita", "approver").role, "approver")
        self.assertEqual(users.set_role(self.conn, self.root, "rita", "reviewer").role, "reviewer")

    def test_root_cannot_be_demoted_or_removed_even_by_another_owner(self):
        olga = self.join("owner", "olga")
        with self.assertRaises(AccountError):
            users.set_role(self.conn, olga, "pshouse", "approver")
        with self.assertRaises(AccountError):
            users.remove(self.conn, olga, "pshouse")

    def test_removal_kills_tokens(self):
        _, code = users.invite(self.conn, self.root, "approver")
        _, token = users.accept_invite(self.conn, code, "abe", "Abe")
        self.assertIsNotNone(users.authenticate(self.conn, token))
        users.remove(self.conn, self.root, "abe")
        self.assertIsNone(users.authenticate(self.conn, token))

    def test_tokens_for_self_but_only_owners_for_others(self):
        abe = self.join("approver", "abe")
        users.new_token(self.conn, abe, "abe")
        with self.assertRaises(AccountError):
            users.new_token(self.conn, abe, "pshouse")

    # --- audit ---

    def test_every_change_is_audited_with_its_actor(self):
        self.join("reviewer", "rita")
        users.set_role(self.conn, self.root, "rita", "approver")
        actions = [(e["actor"], e["action"]) for e in reversed(audit.events(self.conn))]
        self.assertEqual(actions, [
            ("admin:sprite", "user.root_created"),
            ("pshouse", "invite.created"),
            ("rita", "user.joined"),
            ("pshouse", "user.role_changed"),
        ])

    def test_secrets_never_reach_the_log(self):
        _, code = users.invite(self.conn, self.root, "reviewer")
        _, token = users.accept_invite(self.conn, code, "rita", "Rita")
        dump = "\n".join(str(e) for e in audit.events(self.conn))
        for secret in (code, token, self.root_token):
            self.assertNotIn(secret, dump)

    def test_a_failed_change_leaves_no_event(self):
        before = len(audit.events(self.conn))
        with self.assertRaises(AccountError):
            users.set_role(self.conn, self.root, "nobody", "owner")
        self.assertEqual(len(audit.events(self.conn)), before)

    def test_log_refuses_edits_and_deletes(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE audit SET actor = 'x'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM audit")

    def test_verify_catches_tampering_around_the_triggers(self):
        self.join("reviewer", "rita")
        self.join("approver", "abe")
        self.assertTrue(audit.verify(self.conn).ok)
        self.conn.execute("DROP TRIGGER audit_no_update")
        self.conn.execute("UPDATE audit SET actor = 'someone-else' WHERE seq = 2")
        v = audit.verify(self.conn)
        self.assertFalse(v.ok)
        self.assertEqual(v.first_bad, 2)

    def test_verify_catches_a_cut_off_tail(self):
        self.join("reviewer", "rita")
        self.conn.execute("DROP TRIGGER audit_no_delete")
        self.conn.execute("DELETE FROM audit WHERE seq = (SELECT MAX(seq) FROM audit)")
        self.assertFalse(audit.verify(self.conn).ok)


if __name__ == "__main__":
    unittest.main()
