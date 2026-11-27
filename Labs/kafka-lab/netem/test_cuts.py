import unittest

from cuts import CHAIN, pending, restore_input, rule_args

IPS = {"kafka1": "10.0.0.2", "kafka2": "10.0.0.3"}


class CutsTest(unittest.TestCase):
    def test_out_drops_destination(self):
        self.assertEqual(rule_args(IPS, {("kafka2", "out")}), [["-A", CHAIN, "-d", "10.0.0.3", "-j", "DROP"]])

    def test_in_drops_source(self):
        self.assertEqual(rule_args(IPS, {("kafka2", "in")}), [["-A", CHAIN, "-s", "10.0.0.3", "-j", "DROP"]])

    def test_both_directions_out_first(self):
        args = rule_args(IPS, {("kafka2", "in"), ("kafka2", "out"), ("kafka1", "in")})
        self.assertEqual(args, [["-A", CHAIN, "-s", "10.0.0.2", "-j", "DROP"],
                                ["-A", CHAIN, "-d", "10.0.0.3", "-j", "DROP"],
                                ["-A", CHAIN, "-s", "10.0.0.3", "-j", "DROP"]])

    def test_skips_unresolved_peer(self):
        self.assertEqual(rule_args({"runner": None}, {("runner", "out")}), [])

    def test_restore_input_declares_chain_and_rules(self):
        self.assertEqual(restore_input(IPS, {("kafka2", "out")}),
                         f"*filter\n:{CHAIN} - [0:0]\n-A {CHAIN} -d 10.0.0.3 -j DROP\nCOMMIT\n")

    def test_restore_input_empty_clears_chain(self):
        self.assertEqual(restore_input(IPS, set()), f"*filter\n:{CHAIN} - [0:0]\nCOMMIT\n")

    def test_pending_lists_unresolved(self):
        ips = {"kafka1": None, "kafka2": "10.0.0.3", "runner": None}
        cuts = {("kafka1", "out"), ("kafka2", "out"), ("runner", "in")}
        self.assertEqual(pending(ips, cuts), {("kafka1", "out"), ("runner", "in")})


if __name__ == "__main__":
    unittest.main()
