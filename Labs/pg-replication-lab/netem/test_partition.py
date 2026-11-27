import unittest

from partition import CHAIN, needs_reapply, partition_pending, restore_input, rule_commands


class PartitionTest(unittest.TestCase):
    def test_rule_commands_both_directions(self):
        cmds = rule_commands({"replica1": "10.0.0.2", "replica2": "10.0.0.3"}, {"replica2"})
        self.assertEqual(cmds, [["-A", CHAIN, "-s", "10.0.0.3", "-j", "DROP"],
                                ["-A", CHAIN, "-d", "10.0.0.3", "-j", "DROP"]])

    def test_rule_commands_skips_unresolved(self):
        self.assertEqual(rule_commands({"replica1": None}, {"replica1"}), [])

    def test_needs_reapply_on_ip_change(self):
        self.assertTrue(needs_reapply(True, {"replica1": "10.0.0.9"}, {"replica1": "10.0.0.2"}))

    def test_needs_reapply_idle(self):
        self.assertFalse(needs_reapply(False, {"replica1": "10.0.0.9"}, {"replica1": "10.0.0.2"}))

    def test_restore_input_declares_chain_and_rules(self):
        text = restore_input({"replica1": "10.0.0.2", "replica2": "10.0.0.3"}, {"replica2"})
        self.assertEqual(text, f"*filter\n:{CHAIN} - [0:0]\n"
                               f"-A {CHAIN} -s 10.0.0.3 -j DROP\n-A {CHAIN} -d 10.0.0.3 -j DROP\nCOMMIT\n")

    def test_restore_input_empty_still_clears_chain(self):
        self.assertEqual(restore_input({"replica1": "10.0.0.2"}, set()), f"*filter\n:{CHAIN} - [0:0]\nCOMMIT\n")

    def test_partition_pending_for_unresolved(self):
        ips = {"replica1": None, "replica2": "10.0.0.3", "replica3": None}
        self.assertEqual(partition_pending(ips, {"replica1", "replica2"}), {"replica1"})


if __name__ == "__main__":
    unittest.main()
