import unittest


from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        participant = self.rules.validate_create(self.admin, "participants", {"name": "Participant"})
        self.assertEqual(participant["name"], "Participant")
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.admin, "participants", {"name": ""})


if __name__ == "__main__":
    unittest.main()
