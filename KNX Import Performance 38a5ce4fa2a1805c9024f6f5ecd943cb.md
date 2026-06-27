# KNX Import Performance

I inspected the uploaded `.knxproj` file (`KV v2.5 - demo.knxproj`). This is an ETS6 project with a very simple demo installation.

Your BMS importer should ideally extract the following data.

## **Project Information**

| **Field** | **Value** |
| --- | --- |
| Project Name | `KV v2.5 - demo` |
| ETS Version | `ETS6` |
| Tool Version | `6.0.5030.0` |
| Group Address Style | `ThreeLevel` |
| Project ID | `P-03DE` |

---

## **Topology**

### **Area 0**

- Area Address: `0`
- Line Address: `0`

### **Area 1**

- Area Address: `1`
- Line Address: `0`

---

## **KNX Devices**

Your importer should discover **4 devices**.

| **Individual Address** | **Device ID** | **Product Ref** |
| --- | --- | --- |
| `1.0.1` | `P-03DE-0_DI-1` | `M-00FA_H-00FA00250400-1_P-KX.2Etp` |
| `1.0.4` | `P-03DE-0_DI-2` | `M-00FA_H-00FA00250200-1_P-BS.2Etp` |
| `1.0.3` | `P-03DE-0_DI-3` | `M-00FA_H-00FA00250000-1_P-DA.2Etp` |
| `1.0.2` | `P-03DE-0_DI-4` | `M-00FA_H-00FA00250700-1_P-SA.2Etp` |

Note: Device names are empty in this demo project.

---

# **Group Addresses**

The project contains **13 group addresses**.

| **KNX GA** | **Name** | **DPT** |
| --- | --- | --- |
| `0/0/1` | CH-1 - Switching : OnOff | DPST-1-1 |
| `0/0/2` | CH-4 - Switching : OnOff | DPST-1-1 |
| `0/0/3` | CH-4 - Switching : Feedback | DPST-1-1 |
| `0/0/4` | CH-2 - Dimming : OnOff | DPST-1-1 |
| `0/0/5` | CH-2 - Dimming : Dimming Control | DPST-3-7 |
| `0/0/6` | CH-5 - Dimming : OnOff | DPST-1-1 |
| `0/0/7` | CH-5 - Dimming : Dimming Control | DPST-3-7 |
| `0/0/8` | CH-5 - Dimming : Feedback OnOff | DPST-1-1 |
| `0/0/9` | CH-3 - Blinds Control : Move | DPST-1-8 |
| `0/0/10` | CH-3 - Blinds Control : Step/Stop | DPST-1-7 |
| `0/0/11` | CH-6 - Blinds Control : Move | DPST-1-8 |
| `0/0/12` | CH-6 - Blinds Control : Step/Stop | DPST-1-7 |
| `0/0/13` | CH-6 - Blinds Control : Feedback | DPST-5-1 |

---

## **DPT Mapping your BMS should display**

| **DPT** | **Meaning** |
| --- | --- |
| `DPST-1-1` | Boolean Switch (On/Off) |
| `DPST-3-7` | Dimming Control |
| `DPST-1-8` | Move Up/Down |
| `DPST-1-7` | Step/Stop |
| `DPST-5-1` | 8-bit Percentage (0-100%) |

---

## **Minimum JSON Expected From Import**

```json
{
  "projectName": "KV v2.5 - demo",
  "etsVersion": "6.0.5030.0",
  "groupAddressStyle": "ThreeLevel",
  "devices": 4,
  "groupAddresses": 13
}
```

Example:

```json
{
  "address": "0/0/1",
  "name": "CH-1 - Switching : OnOff",
  "dpt": "1.001",
  "type": "switch"
}
```

## **Features a production-grade BMS importer should additionally support**

- Building/Floor/Room hierarchy
- Device manufacturer
- Device serial number
- Communication Objects
- Object flags (R/W/T/U/C/I)
- Group address ↔ Communication Object links
- Parameters
- Tags
- Comments
- Download status
- Individual addresses
- Line couplers / backbone structure

This particular demo project is extremely small and **does not contain room/building hierarchy or rich metadata**, so if your importer only shows 13 group addresses and 4 devices, it is functioning correctly.

Our Output

No. Based on the ETS demo project you uploaded earlier (`KV v2.5 - demo.knxproj`), this extraction does **not look correct**.

A few observations:

### **1. Object count mismatch ❌**

The ETS demo project contains only **13 Group Addresses** and **4 KNX devices**.

Your screenshot shows:

- **40 Objects**
- Names such as:
    - `GF Open Office - Row A Dim`
    - `AHU-01 Supply Air Temp`
    - `1F Meeting Room 1 Brightness`

These names do **not exist** in the demo ETS project.

---

### **2. Device list mismatch ❌**

Your imported device list shows:

- AHU-01
- FCU-01
- FCU-02
- TSTAT-01
- ZC-01
- OAT-01
- CO2-01

The KNX demo project does not contain any of these devices.

Expected devices from the ETS project should look more like:

KNX Individual Address

---

1.0.1

---

1.0.2

---

1.0.3

---

1.0.4

---

or manufacturer device names if present.

---

### **3. Object names mismatch ❌**

Expected Group Addresses:

| **GA** | **Name** |
| --- | --- |
| 0/0/1 | CH-1 - Switching : OnOff |
| 0/0/2 | CH-4 - Switching : OnOff |
| 0/0/3 | CH-4 - Switching : Feedback |
| 0/0/4 | CH-2 - Dimming : OnOff |
| 0/0/5 | CH-2 - Dimming : Dimming Control |
| 0/0/6 | CH-5 - Dimming : OnOff |
| 0/0/7 | CH-5 - Dimming : Dimming Control |
| 0/0/8 | CH-5 - Dimming : Feedback OnOff |
| 0/0/9 | CH-3 - Blinds Control : Move |
| 0/0/10 | CH-3 - Blinds Control : Step/Stop |
| 0/0/11 | CH-6 - Blinds Control : Move |
| 0/0/12 | CH-6 - Blinds Control : Step/Stop |
| 0/0/13 | CH-6 - Blinds Control : Feedback |

---

## **What probably happened**

One of these is likely true:

1. Your UI is still showing **sample BACnet objects** instead of imported KNX objects.
2. You imported the KNX file but are **mapping it to an internal demo dataset**.
3. The parser is reading the file, but the frontend is not displaying the extracted records.
4. The file shown in the screenshot (`knx_ets_sample.xml`) is not the same file you uploaded here.

---

## **What the BMS should ideally show**

### **Device Panel**

```
KNX Device 1 (1.0.1)
KNX Device 2 (1.0.2)
KNX Device 3 (1.0.3)
KNX Device 4 (1.0.4)
```

### **Objects Table**

| **Object Type** | **Name** | **KNX GA** | **DPT** |
| --- | --- | --- | --- |
| binaryValue | CH-1 - Switching : OnOff | 0/0/1 | 1.001 |
| binaryValue | CH-4 - Switching : OnOff | 0/0/2 | 1.001 |
| binaryValue | CH-4 - Switching : Feedback | 0/0/3 | 1.001 |
| multiStateValue | CH-2 - Dimming Control | 0/0/5 | 3.007 |
| binaryValue | CH-3 - Blinds Control : Move | 0/0/9 | 1.008 |

Total objects displayed should be approximately **13**, not 40.

So, based on the screenshot, **your extraction/display pipeline is not showing the actual contents of the uploaded ETS demo project**. The backend parser output would be useful to inspect next. If you can share the extracted JSON/XML after parsing, I can verify whether the parser itself is correct or only the UI mapping is wrong.