import csv
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "sources"
HEADERS = {'pilot_service': ['service', 'zone', 'operator', 'customer', 'offered', 'actual', 'quality', 'service_day', 'evidence_day'], 'customer_commitments': ['offer', 'customer', 'zone', 'operator', 'shared_claim', 'units', 'state', 'known_day', 'alternative_to'], 'investment_options': ['project', 'zone', 'stage', 'period', 'cost', 'deposit', 'capacity', 'dependencies', 'shared_units'], 'funding': ['funder', 'period', 'cash', 'in_kind', 'shared_units'], 'agreements': ['agreement', 'project', 'owner', 'state', 'known_day'], 'stage_events': ['event', 'project', 'known_day', 'reporter', 'observed_capacity', 'service_ref', 'state', 'cash']}
REFERENCES = [['customer_commitments', 'alternative_to', 'customer_commitments', 'offer'], ['investment_options', 'dependencies', 'investment_options', 'project'], ['stage_events', 'project', 'investment_options', 'project'], ['stage_events', 'service_ref', 'pilot_service', 'service']]
COUNTS = [['customer_commitments', 'shared_claim', 'K1', 2], ['stage_events', 'state', 'partial', 2], ['stage_events', 'state', 'deposit', 2]]


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        for name, header in HEADERS.items():
            with (ROOT / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, header)
                self.records[name] = list(reader)

    def test_record_shape(self):
        for name, records in self.records.items():
            with self.subTest(source=name):
                self.assertTrue(records)
                for row in records:
                    self.assertEqual(set(row), set(HEADERS[name]))
                    self.assertNotIn(None, row.values())
                    self.assertTrue(row[HEADERS[name][0]])
                self.assertEqual(len(records), len({tuple(row.values()) for row in records}))

    def test_cross_source_references(self):
        for child, fk, parent, pk in REFERENCES:
            with self.subTest(source=child, field=fk):
                keys = {row[pk] for row in self.records[parent]}
                values = {row[fk] for row in self.records[child] if row[fk]}
                self.assertTrue(values)
                self.assertFalse(values - keys)

    def test_source_has_relevant_observations(self):
        for source, field, value, minimum in COUNTS:
            with self.subTest(source=source, field=field, value=value):
                self.assertGreaterEqual(sum(row[field] == value for row in self.records[source]), minimum)
