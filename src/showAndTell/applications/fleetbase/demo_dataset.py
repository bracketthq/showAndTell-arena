"""Fleetbase-owned reusable preventive-maintenance fixture.

Fleetbase v0.7.52 has no fixture control API.  The pinned application driver
therefore loads these deterministic, marker-owned rows after the upstream
testing seeders.  Fixed UUIDs and unique public IDs make the operation an
idempotent upsert without touching upstream or user-owned records.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

DATASET_PATH = Path(__file__).with_name("demo_dataset.json")
NAMESPACE = UUID("c68ab6c2-2a50-4f42-b472-0a678208b6f8")
MODEL_VEHICLE = r"Fleetbase\FleetOps\Models\Vehicle"


def load_demo_dataset() -> dict[str, Any]:
    dataset = json.loads(DATASET_PATH.read_text())
    if set(dataset) != {"metadata", "fleet"}:
        raise ValueError("Fleetbase demo fixture has unsupported sections")
    metadata = dataset["metadata"]
    if metadata.get("dataset_id") != "enterprise-operations" or metadata.get("version") != 1:
        raise ValueError("Fleetbase demo fixture identity/version is unsupported")
    as_of = date.fromisoformat(str(metadata["as_of_date"]))
    fleet = dataset["fleet"]
    vehicles = {str(row["asset_code"]): row for row in fleet["vehicles"]}
    schedules = {
        str(row["schedule_code"]): row
        for row in fleet["maintenance_schedules"]
    }
    if len(fleet["vehicles"]) != 6 or len(vehicles) != 6:
        raise ValueError("Fleetbase demo fixture needs six unique vehicles")
    if len(fleet["maintenance_schedules"]) != 6 or len(schedules) != 6:
        raise ValueError("Fleetbase demo fixture needs six unique schedules")
    for row in schedules.values():
        if row["asset_code"] not in vehicles or not row["kit_item_code"]:
            raise ValueError(f"invalid maintenance schedule: {row['schedule_code']}")
        date.fromisoformat(str(row["next_due_date"]))
    for row in fleet["work_orders"]:
        if row["asset_code"] not in vehicles or row["schedule_code"] not in schedules:
            raise ValueError(f"invalid work order: {row['work_order_code']}")
        datetime.fromisoformat(str(row["due_at"]))
    for row in fleet["dispatch_orders"]:
        if row["asset_code"] not in vehicles:
            raise ValueError(f"invalid dispatch order: {row['dispatch_code']}")
        datetime.fromisoformat(str(row["scheduled_at"]))
    if schedules["PM-26001"]["next_due_odometer"] - vehicles["BGF-4821"]["odometer"] != 500:
        raise ValueError("Fleetbase fixture lost its exact 500-mile boundary")
    if schedules["PM-26002"]["next_due_odometer"] - vehicles["BGF-4822"]["odometer"] != 501:
        raise ValueError("Fleetbase fixture lost its 501-mile near-boundary")
    if (date.fromisoformat(schedules["PM-26003"]["next_due_date"]) - as_of).days != 7:
        raise ValueError("Fleetbase fixture lost its exact 7-day boundary")
    if (date.fromisoformat(schedules["PM-26004"]["next_due_date"]) - as_of).days != 8:
        raise ValueError("Fleetbase fixture lost its 8-day near-boundary")
    return dataset


def _uuid(kind: str, key: str) -> str:
    return str(uuid5(NAMESPACE, f"{kind}:{key}"))


def _q(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _json(value: Any) -> str:
    return _q(json.dumps(value, separators=(",", ":"), sort_keys=True))


def render_enterprise_seed_sql() -> str:
    dataset = load_demo_dataset()
    fleet = dataset["fleet"]
    dataset_id = dataset["metadata"]["dataset_id"]
    fleet_uuid = _uuid("fleet", "preventive-maintenance")
    lines = [
        "SET @company_uuid := (SELECT uuid FROM companies WHERE name='Bluegrass Freight' LIMIT 1);",
        "INSERT INTO fleets (uuid,public_id,_key,company_uuid,name,color,status,slug,created_at,updated_at) VALUES "
        f"({_q(fleet_uuid)},'fleet_showAndTell_pm','enterprise-operations:fleet',@company_uuid,"
        "'Preventive Maintenance Fleet','#2563EB','active','preventive-maintenance-fleet',UTC_TIMESTAMP(),UTC_TIMESTAMP()) "
        "ON DUPLICATE KEY UPDATE company_uuid=VALUES(company_uuid),name=VALUES(name),status='active',deleted_at=NULL,updated_at=UTC_TIMESTAMP();",
    ]

    vehicle_uuids: dict[str, str] = {}
    for row in fleet["vehicles"]:
        key = str(row["asset_code"])
        vehicle_uuid = _uuid("vehicle", key)
        vehicle_uuids[key] = vehicle_uuid
        meta = {
            "dataset": dataset_id,
            "asset_code": key,
            "scenario_role": row["scenario_role"],
            "vehicle_class": row["vehicle_class"],
        }
        values = [
            _q(vehicle_uuid), _q(f"vehicle_showAndTell_{key.lower().replace('-', '_')}"),
            _q(f"enterprise-operations:{key}"), _q(key), "@company_uuid",
            _q(row["name"]), _q(row["make"]), _q(row["model"]), _q(row["year"]),
            _q(row["vehicle_class"]), _q(row["odometer"]), "'mi'", _q(key),
            _q(f"1STPM{key.replace('-', '')}2026"), _q(row["status"]), "1",
            _json(meta), "UTC_TIMESTAMP()", "UTC_TIMESTAMP()",
        ]
        lines.append(
            "INSERT INTO vehicles (uuid,public_id,_key,internal_id,company_uuid,name,make,model,year,class,odometer,odometer_unit,plate_number,vin,status,online,meta,created_at,updated_at) VALUES ("
            + ",".join(values)
            + ") ON DUPLICATE KEY UPDATE internal_id=VALUES(internal_id),company_uuid=VALUES(company_uuid),name=VALUES(name),make=VALUES(make),model=VALUES(model),year=VALUES(year),class=VALUES(class),odometer=VALUES(odometer),odometer_unit=VALUES(odometer_unit),plate_number=VALUES(plate_number),vin=VALUES(vin),status=VALUES(status),online=VALUES(online),meta=VALUES(meta),deleted_at=NULL,updated_at=UTC_TIMESTAMP();"
        )
        membership_uuid = _uuid("fleet-vehicle", key)
        lines.append(
            "INSERT INTO fleet_vehicles (uuid,_key,fleet_uuid,vehicle_uuid,created_at,updated_at) VALUES ("
            f"{_q(membership_uuid)},{_q(f'enterprise-operations:{key}')},{_q(fleet_uuid)},{_q(vehicle_uuid)},UTC_TIMESTAMP(),UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE fleet_uuid=VALUES(fleet_uuid),vehicle_uuid=VALUES(vehicle_uuid),deleted_at=NULL,updated_at=UTC_TIMESTAMP();"
        )

    schedule_uuids: dict[str, str] = {}
    for row in fleet["maintenance_schedules"]:
        key = str(row["schedule_code"])
        schedule_uuid = _uuid("maintenance-schedule", key)
        schedule_uuids[key] = schedule_uuid
        meta = {
            "dataset": dataset_id,
            "schedule_code": key,
            "kit_item_code": row["kit_item_code"],
        }
        lines.append(
            "INSERT INTO maintenance_schedules (uuid,public_id,_key,company_uuid,subject_type,subject_uuid,name,type,status,interval_method,interval_type,interval_value,interval_unit,next_due_date,next_due_odometer,default_priority,instructions,reminder_offsets,meta,created_at,updated_at) VALUES ("
            + ",".join([
                _q(schedule_uuid), _q(f"schedule_showAndTell_{key.lower().replace('-', '_')}"),
                _q(f"enterprise-operations:{key}"), "@company_uuid", _q(MODEL_VEHICLE),
                _q(vehicle_uuids[str(row["asset_code"])]), _q(row["name"]), "'service'",
                _q(row["status"]), "'hybrid'", "'recurring'", "1", "'years'",
                _q(f"{row['next_due_date']} 09:00:00"), _q(row["next_due_odometer"]),
                "'normal'", _q(f"Use ERPNext kit {row['kit_item_code']} before release."),
                "'[15,7,3]'", _json(meta), "UTC_TIMESTAMP()", "UTC_TIMESTAMP()",
            ])
            + ") ON DUPLICATE KEY UPDATE company_uuid=VALUES(company_uuid),subject_type=VALUES(subject_type),subject_uuid=VALUES(subject_uuid),name=VALUES(name),type=VALUES(type),status=VALUES(status),next_due_date=VALUES(next_due_date),next_due_odometer=VALUES(next_due_odometer),instructions=VALUES(instructions),meta=VALUES(meta),deleted_at=NULL,updated_at=UTC_TIMESTAMP();"
        )

    for row in fleet["work_orders"]:
        key = str(row["work_order_code"])
        closed_at = "UTC_TIMESTAMP()" if row["status"] == "completed" else "NULL"
        meta = {"dataset": dataset_id, "scenario_role": row["scenario_role"]}
        lines.append(
            "INSERT INTO work_orders (uuid,public_id,_key,company_uuid,schedule_uuid,code,subject,status,priority,target_type,target_uuid,opened_at,due_at,closed_at,estimated_duration_hours,resource_requirements,currency,instructions,meta,created_at,updated_at) VALUES ("
            + ",".join([
                _q(_uuid("work-order", key)), _q(f"work_order_showAndTell_{key.lower().replace('-', '_')}"),
                _q(f"enterprise-operations:{key}"), "@company_uuid",
                _q(schedule_uuids[str(row["schedule_code"])]), _q(key), _q(row["subject"]),
                _q(row["status"]), _q(row["priority"]), _q(MODEL_VEHICLE),
                _q(vehicle_uuids[str(row["asset_code"])]), "'2026-08-05 08:00:00'",
                _q(row["due_at"]), closed_at, "4", _json({"kit_required": True}),
                "'USD'", "'Verify parts readiness before changing vehicle availability.'",
                _json(meta), "UTC_TIMESTAMP()", "UTC_TIMESTAMP()",
            ])
            + ") ON DUPLICATE KEY UPDATE company_uuid=VALUES(company_uuid),schedule_uuid=VALUES(schedule_uuid),code=VALUES(code),subject=VALUES(subject),status=VALUES(status),priority=VALUES(priority),target_type=VALUES(target_type),target_uuid=VALUES(target_uuid),due_at=VALUES(due_at),closed_at=VALUES(closed_at),resource_requirements=VALUES(resource_requirements),meta=VALUES(meta),deleted_at=NULL,updated_at=UTC_TIMESTAMP();"
        )

    for row in fleet["dispatch_orders"]:
        key = str(row["dispatch_code"])
        meta = {"dataset": dataset_id, "dispatch_code": key, "scenario_role": row["scenario_role"]}
        lines.append(
            "INSERT INTO orders (uuid,public_id,_key,company_uuid,internal_id,vehicle_assigned_uuid,meta,dispatched,started,adhoc,scheduled_at,type,status,created_at,updated_at) VALUES ("
            + ",".join([
                _q(_uuid("dispatch-order", key)), _q(f"order_showAndTell_{key.lower().replace('-', '_')}"),
                _q(f"enterprise-operations:{key}"), "@company_uuid", _q(key),
                _q(vehicle_uuids[str(row["asset_code"])]), _json(meta), "0", "0", "1",
                _q(row["scheduled_at"]), "'transport'", _q(row["status"]),
                "UTC_TIMESTAMP()", "UTC_TIMESTAMP()",
            ])
            + ") ON DUPLICATE KEY UPDATE company_uuid=VALUES(company_uuid),internal_id=VALUES(internal_id),vehicle_assigned_uuid=VALUES(vehicle_assigned_uuid),meta=VALUES(meta),scheduled_at=VALUES(scheduled_at),status=VALUES(status),deleted_at=NULL,updated_at=UTC_TIMESTAMP();"
        )

    return "\n".join(lines) + "\n"
