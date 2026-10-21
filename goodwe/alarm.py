"""Inverter alarm (event) log records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .const import ALARM_TYPES_ET, BMS_ALARM_CODES, BMS_WARNING_CODES, ERROR_CODES, WORK_MODES_ET
from .sensor import decode_bitmap

# Alarm types the SolarGo app displays (and only when the code is non-zero)
_VISIBLE_ALARM_TYPES = (1, 3, 4)


@dataclass
class AlarmRecord:
    """Single record of the inverter alarm log"""

    id: int
    timestamp: datetime
    """Inverter local time (naive datetime, the inverter does not store any timezone)"""
    code: int
    type: int
    type_label: str
    status: int
    """Inverter work mode at the time of the alarm"""
    status_label: str
    temperature: int
    description: str
    unknown: int
    """Last 2 bytes of the record, meaning not known yet"""
    hidden: bool
    """
    True for records the SolarGo app does not display: code 0 (all alarms of the type cleared)
    or type 2 (warning_code register 35185, bit meanings unknown so description is empty)
    """

    @classmethod
    def from_et_bytes(cls, data: bytes) -> AlarmRecord | None:
        """
        Decode 16 byte ET alarm log record, answer None for an empty (0xFF filled) slot.
        data[0:4] timestamp, data[4:8] code, data[8] type, data[9] status (work mode),
        data[10] 0xFF, data[11] temperature, data[12:14] id, data[14:16] unknown
        """
        if data == b"\xff" * 16:
            return None
        code = int.from_bytes(data[4:8], "big")
        type_ = data[8]
        if type_ == 1:
            description = decode_bitmap(code, ERROR_CODES)
        elif type_ == 3:
            description = decode_bitmap(code, BMS_ALARM_CODES)
        elif type_ == 4:
            description = decode_bitmap(code, BMS_WARNING_CODES)
        else:
            description = ""
        return cls(
            id=int.from_bytes(data[12:14], "big"),
            timestamp=datetime.fromtimestamp(
                int.from_bytes(data[0:4], "big"), timezone.utc
            ).replace(tzinfo=None),
            code=code,
            type=type_,
            type_label=ALARM_TYPES_ET.get(type_, f"Unknown ({type_})"),
            status=data[9],
            status_label=WORK_MODES_ET.get(data[9], f"Unknown ({data[9]})"),
            temperature=int.from_bytes(data[11:12], "big", signed=True),
            description=description,
            unknown=int.from_bytes(data[14:16], "big"),
            hidden=code == 0 or type_ not in _VISIBLE_ALARM_TYPES,
        )
