#!/usr/bin/env python3
"""Manage a native Home Assistant power-monitoring dashboard."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
DEFAULT_BACKUP = Path(__file__).with_name("rollback.json")


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Replace a JSON file atomically, leaving the previous record on failure."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    dashboard = data.get("dashboard", {})
    plugs = data.get("plugs", [])
    required_dashboard = ("title", "url_path", "icon")
    missing = [key for key in required_dashboard if not dashboard.get(key)]
    if missing:
        raise ValueError(f"Missing dashboard fields: {', '.join(missing)}")
    if "-" not in dashboard["url_path"]:
        raise ValueError("dashboard.url_path must contain a hyphen")
    if not plugs:
        raise ValueError("At least one plug is required")
    names: set[str] = set()
    entities: set[str] = set()
    for index, plug in enumerate(plugs, start=1):
        for field in ("name", "power_entity", "energy_entity"):
            if not plug.get(field):
                raise ValueError(f"Plug {index} is missing {field}")
        if plug["name"] in names:
            raise ValueError(f"Duplicate plug name: {plug['name']}")
        names.add(plug["name"])
        for field in ("power_entity", "energy_entity"):
            entity = plug[field]
            if not ENTITY_ID_RE.fullmatch(entity):
                raise ValueError(f"Invalid entity ID: {entity}")
            if entity in entities:
                raise ValueError(f"Duplicate entity ID: {entity}")
            entities.add(entity)
    return data


def load_credentials() -> tuple[str, str, Any]:
    """Return URL, token and an optional Hermes scope cleanup callback."""
    url = os.environ.get("HASS_URL")
    token = os.environ.get("HASS_TOKEN")
    if token:
        return (url or "http://homeassistant.local:8123").rstrip("/"), token, None

    try:
        from agent.secret_scope import (  # type: ignore[import-not-found]
            build_profile_secret_scope,
            get_secret,
            reset_secret_scope,
            set_secret_scope,
        )
    except ImportError as exc:
        raise RuntimeError(
            "HASS_TOKEN is unavailable; use a protected environment or run on a configured Hermes host"
        ) from exc

    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    scope_token = set_secret_scope(build_profile_secret_scope(hermes_home))
    url = get_secret("HASS_URL", "http://homeassistant.local:8123")
    token = get_secret("HASS_TOKEN", "")
    if not token:
        reset_secret_scope(scope_token)
        raise RuntimeError("HASS_TOKEN is not configured in the active Hermes profile")
    return (url or "http://homeassistant.local:8123").rstrip("/"), token, lambda: reset_secret_scope(scope_token)


class HomeAssistantWS:
    def __init__(self) -> None:
        base_url, self.token, self.cleanup = load_credentials()
        self.ws_url = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/websocket"
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.next_id = 1
        self.ha_version = "unknown"

    async def __aenter__(self) -> "HomeAssistantWS":
        try:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            self.ws = await self.session.ws_connect(self.ws_url, heartbeat=20)
            greeting = await self.ws.receive_json()
            if greeting.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected WebSocket greeting: {greeting.get('type')}")
            await self.ws.send_json({"type": "auth", "access_token": self.token})
            auth = await self.ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant WebSocket authentication failed")
            self.ha_version = auth.get("ha_version", "unknown")
            return self
        except Exception:
            await self._close()
            raise

    async def __aexit__(self, *_: object) -> None:
        await self._close()

    async def _close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        if self.session is not None:
            await self.session.close()
            self.session = None
        if self.cleanup is not None:
            self.cleanup()
            self.cleanup = None

    async def call(self, command_type: str, **payload: Any) -> Any:
        if self.ws is None:
            raise RuntimeError("WebSocket is not connected")
        message_id = self.next_id
        self.next_id += 1
        await self.ws.send_json({"id": message_id, "type": command_type, **payload})
        while True:
            response = await self.ws.receive_json()
            if response.get("id") != message_id:
                continue
            if not response.get("success"):
                error = response.get("error", {})
                raise RuntimeError(f"{command_type}: {error.get('code')}: {error.get('message')}")
            return response.get("result")


def entity_rows(config: dict[str, Any], field: str) -> list[dict[str, str]]:
    return [{"entity": plug[field], "name": plug["name"]} for plug in config["plugs"]]


def entity_ids(config: dict[str, Any], field: str) -> list[str]:
    return [plug[field] for plug in config["plugs"]]


def statistics_card(
    config: dict[str, Any], title: str, field: str, period: str, days: int,
    stat_types: list[str], chart_type: str,
) -> dict[str, Any]:
    return {
        "type": "vertical-stack",
        "cards": [
            {"type": "heading", "heading": title, "heading_style": "title"},
            {
                "type": "statistics-graph",
                "entities": entity_rows(config, field),
                "period": period,
                "days_to_show": days,
                "stat_types": stat_types,
                "chart_type": chart_type,
                "hide_legend": False,
            },
        ],
    }


def energy_statistic_grid(config: dict[str, Any], title: str, period: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "vertical-stack",
        "cards": [
            {"type": "heading", "heading": title, "heading_style": "title"},
            {
                "type": "grid",
                "columns": 2,
                "square": False,
                "cards": [
                    {
                        "type": "statistic",
                        "entity": plug["energy_entity"],
                        "name": plug["name"],
                        "stat_type": "change",
                        "period": period,
                    }
                    for plug in config["plugs"]
                ],
            },
        ],
    }


def build_view(
    config: dict[str, Any], title: str, path: str, period: str, days: int,
    description: str,
) -> dict[str, Any]:
    icons = {
        "hour": "mdi:clock-outline",
        "day": "mdi:calendar-today",
        "month": "mdi:calendar-month",
        "year": "mdi:calendar-range",
    }
    if path == "hour":
        return {
            "title": title,
            "path": path,
            "icon": icons[path],
            "cards": [
                {"type": "markdown", "content": "## Hour view\nEnergy consumed and power activity over the latest rolling hour."},
                energy_statistic_grid(
                    config,
                    "Energy consumed — latest hour",
                    {"rolling_window": {"duration": {"hours": 1}}},
                ),
                {
                    "type": "history-graph",
                    "title": "Power — latest hour",
                    "hours_to_show": 1,
                    "entities": entity_rows(config, "power_entity"),
                },
            ],
        }

    rolling_periods = {
        "day": ({"rolling_window": {"duration": {"hours": 24}}}, "latest day"),
        "month": ({"rolling_window": {"duration": {"days": 31}}}, "latest 31 days"),
        "year": ({"rolling_window": {"duration": {"days": 365}}}, "latest year"),
    }
    statistic_period, range_label = rolling_periods[path]
    return {
        "title": title,
        "path": path,
        "icon": icons[path],
        "cards": [
            {
                "type": "markdown",
                "content": (
                    f"## {title} view\n{description}\n\nEnergy values are consumption changes "
                    f"for the {range_label}, never cumulative meter totals."
                ),
            },
            energy_statistic_grid(
                config,
                f"Energy consumed — {range_label}",
                statistic_period,
            ),
            statistics_card(config, "Power", "power_entity", period, days, ["mean", "max"], "line"),
            statistics_card(config, "Energy consumed", "energy_entity", period, days, ["change"], "bar"),
        ],
    }


def dashboard_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": config["dashboard"]["title"],
        "views": [
            build_view(config, "Hour", "hour", "5minute", 1, "Latest hour."),
            build_view(config, "Day", "day", "hour", 1, "Hourly statistics covering the latest 24 hours."),
            build_view(config, "Month", "month", "day", 31, "Daily statistics covering the latest 31 days."),
            build_view(config, "Year", "year", "month", 365, "Monthly statistics covering the latest 365 days."),
        ],
    }


async def statistics_counts(ha: HomeAssistantWS, config: dict[str, Any], days: int, period: str) -> dict[str, int]:
    ids = entity_ids(config, "power_entity") + entity_ids(config, "energy_entity")
    stats = await ha.call(
        "recorder/statistics_during_period",
        start_time=(datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
        statistic_ids=ids,
        period=period,
        types=["mean", "max", "change", "state", "sum"],
    )
    return {entity: len(stats.get(entity, [])) for entity in ids}


async def probe(config: dict[str, Any], _: Path) -> None:
    async with HomeAssistantWS() as ha:
        dashboards = await ha.call("lovelace/dashboards/list")
        counts = await statistics_counts(ha, config, 1, "hour")
        print(json.dumps({
            "ha_version": ha.ha_version,
            "websocket_auth": "ok",
            "dashboard_admin_api": "readable",
            "configured_entities": len(counts),
            "entities_with_statistics": sum(value > 0 for value in counts.values()),
            "dashboard_path_available": all(
                item.get("url_path") != config["dashboard"]["url_path"] for item in dashboards
            ),
        }, indent=2))


async def create(config: dict[str, Any], backup_path: Path) -> None:
    dashboard = config["dashboard"]
    async with HomeAssistantWS() as ha:
        dashboards = await ha.call("lovelace/dashboards/list")
        if any(item.get("url_path") == dashboard["url_path"] for item in dashboards):
            raise RuntimeError(f"Dashboard already exists: {dashboard['url_path']}")
        backup = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ha_version": ha.ha_version,
            "dashboard_path": dashboard["url_path"],
            "dashboard_existed": False,
            "status": "creation_pending",
        }
        # This must succeed before Home Assistant state is changed. The URL path
        # is sufficient for rollback even if persisting the generated ID fails.
        atomic_write_json(backup_path, backup)
        created = await ha.call(
            "lovelace/dashboards/create",
            url_path=dashboard["url_path"],
            title=dashboard["title"],
            icon=dashboard["icon"],
            show_in_sidebar=True,
            require_admin=False,
        )
        try:
            backup["created_dashboard_id"] = created["id"]
            backup["status"] = "dashboard_created"
            atomic_write_json(backup_path, backup)
            await ha.call("lovelace/config/save", url_path=dashboard["url_path"], config=dashboard_config(config))
            backup["status"] = "active"
            atomic_write_json(backup_path, backup)
        except Exception as operation_error:
            try:
                await ha.call("lovelace/dashboards/delete", dashboard_id=created["id"])
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"Dashboard setup failed ({operation_error}); cleanup also failed "
                    f"({cleanup_error}). Use {backup_path} to retry rollback."
                ) from operation_error
            raise RuntimeError(
                f"Dashboard setup failed; the created dashboard was removed: {operation_error}"
            ) from operation_error
        print(json.dumps({"created": created, "rollback_file": str(backup_path)}, indent=2))


async def update(config: dict[str, Any], _: Path) -> None:
    dashboard = config["dashboard"]
    async with HomeAssistantWS() as ha:
        dashboards = await ha.call("lovelace/dashboards/list")
        existing = next((item for item in dashboards if item.get("url_path") == dashboard["url_path"]), None)
        if not existing:
            raise RuntimeError("Configured dashboard was not found")
        await ha.call("lovelace/config/save", url_path=dashboard["url_path"], config=dashboard_config(config))
        print(json.dumps({"updated_dashboard_id": existing["id"], "config": "saved"}, indent=2))


async def verify(config: dict[str, Any], _: Path) -> None:
    dashboard = config["dashboard"]
    expected_ids = entity_ids(config, "power_entity") + entity_ids(config, "energy_entity")
    async with HomeAssistantWS() as ha:
        dashboards = await ha.call("lovelace/dashboards/list")
        existing = next((item for item in dashboards if item.get("url_path") == dashboard["url_path"]), None)
        if not existing:
            raise RuntimeError("Configured dashboard was not found")
        saved = await ha.call("lovelace/config", url_path=dashboard["url_path"])
        serialized = json.dumps(saved)
        failures = []
        if [view.get("path") for view in saved.get("views", [])] != ["hour", "day", "month", "year"]:
            failures.append("unexpected dashboard views")
        missing = [entity for entity in expected_ids if entity not in serialized]
        if missing:
            failures.append(f"missing entities: {missing}")
        if '"type": "entities"' in serialized:
            failures.append("unexpected current-readings card")
        if '"type": "energy-date-selection"' in serialized:
            failures.append("unexpected generic date selector")
        if serialized.count('"columns": 2') != 4:
            failures.append("expected two-column consumption grids")
        counts = await statistics_counts(ha, config, 365, "month")
        missing_statistics = [entity for entity, count in counts.items() if count == 0]
        if missing_statistics:
            failures.append(f"entities without long-term statistics: {missing_statistics}")
        if failures:
            raise RuntimeError("; ".join(failures))
        print(json.dumps({
            "dashboard": existing,
            "views": ["hour", "day", "month", "year"],
            "entity_references": len(expected_ids),
            "entities_with_long_term_statistics": sum(value > 0 for value in counts.values()),
            "verification": "passed",
        }, indent=2))


async def rollback(_: dict[str, Any], backup_path: Path) -> None:
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    dashboard_id = backup.get("created_dashboard_id")
    async with HomeAssistantWS() as ha:
        if not dashboard_id:
            dashboard_path = backup.get("dashboard_path")
            dashboards = await ha.call("lovelace/dashboards/list")
            dashboard = next(
                (item for item in dashboards if item.get("url_path") == dashboard_path),
                None,
            )
            if dashboard is None:
                print(json.dumps({"dashboard_path": dashboard_path, "rollback": "already absent"}, indent=2))
                return
            dashboard_id = dashboard["id"]
        await ha.call("lovelace/dashboards/delete", dashboard_id=dashboard_id)
        print(json.dumps({"deleted_dashboard_id": dashboard_id, "rollback": "complete"}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    parser.add_argument("command", choices=("probe", "create", "update", "verify", "rollback"))
    args = parser.parse_args()
    config = load_config(args.config)
    asyncio.run(globals()[args.command](config, args.backup))


if __name__ == "__main__":
    main()
