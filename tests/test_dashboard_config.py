import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ha_power_dashboard import create, dashboard_config, load_config, rollback


VALID = {
    "dashboard": {
        "title": "Power Monitoring",
        "url_path": "dashboard-power-monitoring",
        "icon": "mdi:chart-line",
    },
    "plugs": [
        {
            "name": "Test plug",
            "power_entity": "sensor.test_plug_power",
            "energy_entity": "sensor.test_plug_energy",
        }
    ],
}


class DashboardConfigTests(unittest.TestCase):
    def write_config(self, data):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        with handle:
            json.dump(data, handle)
        return Path(handle.name)

    def test_load_and_build_expected_views(self):
        config = load_config(self.write_config(VALID))
        generated = dashboard_config(config)
        self.assertEqual([view["path"] for view in generated["views"]], ["hour", "day", "month", "year"])

    def test_no_current_readings_or_generic_date_picker(self):
        generated = json.dumps(dashboard_config(VALID))
        self.assertNotIn('"type": "entities"', generated)
        self.assertNotIn('"type": "energy-date-selection"', generated)
        self.assertEqual(generated.count('"stat_type": "change"'), 4)

    def test_periods_match_views(self):
        views = {view["path"]: view for view in dashboard_config(VALID)["views"]}
        hour_period = views["hour"]["cards"][1]["cards"][1]["cards"][0]["period"]
        day_period = views["day"]["cards"][1]["cards"][1]["cards"][0]["period"]
        month_period = views["month"]["cards"][1]["cards"][1]["cards"][0]["period"]
        year_period = views["year"]["cards"][1]["cards"][1]["cards"][0]["period"]
        self.assertEqual(hour_period, {"rolling_window": {"duration": {"hours": 1}}})
        self.assertEqual(day_period, {"rolling_window": {"duration": {"hours": 24}}})
        self.assertEqual(month_period, {"rolling_window": {"duration": {"days": 31}}})
        self.assertEqual(year_period, {"rolling_window": {"duration": {"days": 365}}})

        day_graph = views["day"]["cards"][2]["cards"][1]
        month_graph = views["month"]["cards"][2]["cards"][1]
        year_graph = views["year"]["cards"][2]["cards"][1]
        self.assertEqual(day_graph["period"], "hour")
        self.assertEqual(day_graph["days_to_show"], 1)
        self.assertEqual(month_graph["period"], "day")
        self.assertEqual(month_graph["days_to_show"], 31)
        self.assertEqual(year_graph["period"], "month")
        self.assertEqual(year_graph["days_to_show"], 365)

    def test_energy_graphs_use_change_and_power_graphs_use_mean_and_max(self):
        for view in dashboard_config(VALID)["views"][1:]:
            power_graph, energy_graph = [section["cards"][1] for section in view["cards"][2:]]
            self.assertEqual(power_graph["stat_types"], ["mean", "max"])
            self.assertEqual(energy_graph["stat_types"], ["change"])

    def test_statistics_graphs_use_external_headings_without_history_links(self):
        for view in dashboard_config(VALID)["views"][1:]:
            for section in view["cards"][2:]:
                self.assertEqual(section["type"], "vertical-stack")
                self.assertEqual(section["cards"][0]["type"], "heading")
                graph = section["cards"][1]
                self.assertEqual(graph["type"], "statistics-graph")
                self.assertNotIn("title", graph)

    def test_rejects_duplicate_entities(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["plugs"].append({
            "name": "Second plug",
            "power_entity": "sensor.test_plug_power",
            "energy_entity": "sensor.second_plug_energy",
        })
        with self.assertRaisesRegex(ValueError, "Duplicate entity ID"):
            load_config(self.write_config(invalid))

    def test_rejects_invalid_entity_id(self):
        invalid = json.loads(json.dumps(VALID))
        invalid["plugs"][0]["power_entity"] = "../../bad"
        with self.assertRaisesRegex(ValueError, "Invalid entity ID"):
            load_config(self.write_config(invalid))


class FakeHomeAssistant:
    def __init__(self, dashboards=None):
        self.dashboards = dashboards or []
        self.calls = []
        self.ha_version = "test"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def call(self, command_type, **payload):
        self.calls.append((command_type, payload))
        if command_type == "lovelace/dashboards/list":
            return self.dashboards
        if command_type == "lovelace/dashboards/create":
            return {"id": "created_dashboard"}
        return None


class LifecycleTests(unittest.TestCase):
    def test_precreation_record_failure_prevents_creation(self):
        ha = FakeHomeAssistant()
        with patch("ha_power_dashboard.HomeAssistantWS", return_value=ha), patch(
            "ha_power_dashboard.atomic_write_json", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                asyncio.run(create(VALID, Path("rollback.json")))
        self.assertNotIn("lovelace/dashboards/create", [call[0] for call in ha.calls])

    def test_postcreation_record_failure_deletes_dashboard(self):
        ha = FakeHomeAssistant()
        with patch("ha_power_dashboard.HomeAssistantWS", return_value=ha), patch(
            "ha_power_dashboard.atomic_write_json", side_effect=[None, OSError("disk full")]
        ):
            with self.assertRaisesRegex(RuntimeError, "created dashboard was removed"):
                asyncio.run(create(VALID, Path("rollback.json")))
        commands = [call[0] for call in ha.calls]
        self.assertIn("lovelace/dashboards/create", commands)
        self.assertIn("lovelace/dashboards/delete", commands)

    def test_rollback_resolves_dashboard_by_precreated_path(self):
        backup = self.write_backup({"dashboard_path": "dashboard-power-monitoring"})
        ha = FakeHomeAssistant([
            {"id": "resolved_dashboard", "url_path": "dashboard-power-monitoring"}
        ])
        with patch("ha_power_dashboard.HomeAssistantWS", return_value=ha):
            asyncio.run(rollback(VALID, backup))
        delete_call = next(call for call in ha.calls if call[0] == "lovelace/dashboards/delete")
        self.assertEqual(delete_call[1]["dashboard_id"], "resolved_dashboard")

    @staticmethod
    def write_backup(data):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        with handle:
            json.dump(data, handle)
        return Path(handle.name)


if __name__ == "__main__":
    unittest.main()
