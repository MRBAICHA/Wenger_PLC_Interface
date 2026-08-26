"""
test_read_plc.py
------------------
ทดสอบอ่านค่า tag จาก PLC Wenger Extruder (EX05, Allen-Bradley ControlLogix) อย่างเดียว
(read-only, ไม่มีการเขียนค่าใด ๆ กลับไปที่ PLC และไม่เก็บลง database) — ใช้เช็คว่าเชื่อมต่อได้
และ tag name ยังตรงก่อนรันตัวเก็บ log จริง

ยืนยันแล้วว่า get_tag_list() ใช้งานได้ปกติ (คืน 3531 tag) — ที่เคยดูเหมือน "โดนบล็อก" ก่อนหน้านี้
ไม่ใช่ปัญหาจริง และชุด sensor ด้านล่างนี้ verified เทียบกับหน้าจอ SCADA จริงแล้ว (2026-08-16)
ตรงเกือบเป๊ะทุกตัว (ConeHead, CylSteamPress, LubeOilTemp, AirPress ตรงเป๊ะ; Temp/SteamPress
ต่างกันเพราะค่าขยับระหว่างอ่านสองรอบ) ยกเว้น "Hygenic Hood Pressure" บนจอจริง map ไปที่
Wen_FilterFanPressure ไม่ใช่ tag ชื่อ HygenicHood ตรง ๆ (ตัวนั้นอ่านได้ 0.0 ค้าง ไม่ใช้งานจริง)

รัน:
    python test_read_plc.py
"""

import json
from pycomm3 import LogixDriver

PLC_IP = "10.232.145.90"  # Wenger Extruder — EX05

# tag ที่มีความหมายจริงสำหรับ production overview (คัดจาก tag ทั้งหมด 3531 ตัว)
RUN_TAGS = [
    "Wen_ProductionPhase",  # BOOL  กำลังผลิตอยู่จริงหรือไม่
    "Wen_ExtruderRate",     # REAL  อัตราการผลิต (kg/h)
    "Wen_ExtruderSpeed",    # struct (CM_AI) .PV = ความเร็วสกรูจริง (RPM)
    "ProductFormulaExtruder",  # struct (WengerRecipe) สูตรที่กำลังรันอยู่จริง
    "Extruder_Status",      # DINT  status word
    "RunAlarm",             # BOOL  มี alarm รันอยู่หรือไม่
]

# sensor ทางกายภาพ — verified ตรงกับหน้าจอ SCADA จริงแล้ว, ทุกตัวเป็น struct (CM_AI/InputDisp)
SENSOR_TAGS = {
    "Wen_ExtrudateTemp1":   ("Extruder Temp.1 (°C)", "PV"),
    "Wen_ExtrudateTemp2":   ("Extruder Temp.2 (°C)", "PV"),
    "Wen_KnifehoodTemp":    ("KnifeHood Hood (°C)", "PV"),
    "Wen_KnifehoodCoil":    ("KnifeHood Coil (°C)", "PV"),
    "Wen_KnifehoodCyclone": ("KnifeHood Cyclone (°C)", "PV"),
    "Wen_ConeHead":         ("Conehead (bar)", "PV"),
    "Wen_SteamPress":       ("System Steam (bar)", "PV"),
    "Wen_CylSteamPress":    ("Cylinder Steam (bar)", "PV"),
    "Wen_ExtSteamPress":    ("Extruder Steam (bar)", "PV"),
    "Wen_WaterPress":       ("System Water (bar)", "PV"),
    "Wen_AirPress":         ("System Air (bar)", "PV"),
    "Wen_LubeOilTemp":      ("Lube Oil (°C)", "PV"),
    "Wen_FilterFanPressure": ("Hygenic Hood Pressure (bar)", "PV"),
    "ExtruderLoad":         ("Motor Load (kW)", "PVEngr"),
}

TAGS = RUN_TAGS + list(SENSOR_TAGS.keys())


def main():
    print(f"กำลังเชื่อมต่อ PLC ที่ {PLC_IP} (Wenger Extruder EX05) ...")
    with LogixDriver(PLC_IP) as plc:
        if not plc.connected:
            print("เชื่อมต่อ PLC ไม่สำเร็จ ตรวจสอบ IP/network")
            return

        info = plc.info
        print(f"เชื่อมต่อสำเร็จ: {info.get('product_name', 'unknown')} "
              f"(name: {info.get('name', '?')}, rev {info.get('revision', '?')}, keyswitch: {info.get('keyswitch', '?')})\n")

        results = plc.read(*TAGS)
        by_tag = {r.tag: r for r in results}

        prod = by_tag["Wen_ProductionPhase"]
        rate = by_tag["Wen_ExtruderRate"]
        speed = by_tag["Wen_ExtruderSpeed"]
        formula = by_tag["ProductFormulaExtruder"]
        status = by_tag["Extruder_Status"]
        alarm = by_tag["RunAlarm"]

        print(f"{'กำลังผลิต (Wen_ProductionPhase)':<45} {prod.value!r}")
        print(f"{'อัตราการผลิต (Wen_ExtruderRate)':<45} {rate.value} kg/h")
        print(f"{'ความเร็วสกรูจริง (Wen_ExtruderSpeed.PV)':<45} {speed.value.get('PV') if speed.value else None}")
        print(f"{'Status word (Extruder_Status)':<45} {status.value}")
        print(f"{'Run Alarm':<45} {alarm.value!r}")

        print("\n--- Sensor readouts (verified against real SCADA screen) ---")
        json_out = {
            "Wen_ProductionPhase": prod.value,
            "Wen_ExtruderRate": rate.value,
            "Wen_ExtruderSpeed.PV": speed.value.get("PV") if speed.value else None,
            "Extruder_Status": status.value,
            "RunAlarm": alarm.value,
        }
        for tag, (label, field) in SENSOR_TAGS.items():
            r = by_tag[tag]
            val = r.value.get(field) if isinstance(r.value, dict) else r.value
            print(f"  {label:<32} {val}")
            json_out[f"{tag}.{field}"] = val

        print()
        if formula.value:
            f = formula.value
            print("สูตรที่กำลังรันอยู่ (ProductFormulaExtruder):")
            print(f"  FormulaID    : {f.get('FormulaID')}")
            print(f"  Description  : {f.get('Description')}")
            print(f"  Run_Number   : {f.get('Run_Number')}")
            print(f"  Revision     : {f.get('Revision')}")
            print(f"  Die_Num      : {f.get('Die_Num')}")
            print(f"  FormDate     : {f.get('FormDate')}")
            json_out["ProductFormulaExtruder.FormulaID"] = f.get("FormulaID")
            json_out["ProductFormulaExtruder.Description"] = f.get("Description")
        else:
            print(f"อ่าน ProductFormulaExtruder ไม่ได้: {formula.error}")

        print("\n--- raw JSON (สำหรับ debug) ---")
        print(json.dumps(json_out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
