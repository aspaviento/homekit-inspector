import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_inspector import (  # noqa: E402
    load_accessory_versions, load_home_layout, load_infrastructure, load_scenes,
    service_capability, version_text,
)
from homed_extract import (  # noqa: E402
    _build_accessory_lookup,
    _build_characteristic_lookup,
    _build_room_lookup,
    _build_service_lookup,
    _build_zone_lookup,
    _extract_home_selection,
)


class MultiHomeTests(unittest.TestCase):
    def test_version_fields_preserve_sources_and_filter_homes(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript("""
            CREATE TABLE ZMKFACCESSORY (
                Z_PK INTEGER, ZHOME INTEGER, ZSOFTWAREVERSION BLOB,
                ZFIRMWAREVERSION TEXT, ZDISPLAYABLEFIRMWAREVERSION TEXT
            );
            INSERT INTO ZMKFACCESSORY VALUES
                (1, 1, X'32372E32', '18.0', NULL),
                (2, 1, NULL, '16973889.0', '1.3.0'),
                (3, 2, NULL, '2.0', NULL);
        """)
        versions = load_accessory_versions(connection, 1)
        self.assertEqual(set(versions), {1, 2})
        self.assertEqual(versions[1]["softwareVersion"], "27.2")
        self.assertEqual(versions[1]["firmwareVersion"], "18.0")
        self.assertEqual(versions[2]["firmwareVersion"], "1.3.0")
        self.assertEqual(versions[2]["rawFirmwareVersion"], "16973889.0")
        self.assertEqual(versions[2]["firmwareVersionSource"], "ZDISPLAYABLEFIRMWAREVERSION")
        connection.close()

    def test_version_fields_are_optional_in_older_schemas(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE ZMKFACCESSORY (Z_PK INTEGER, ZHOME INTEGER)")
        connection.execute("INSERT INTO ZMKFACCESSORY VALUES (1, 1)")
        self.assertEqual(load_accessory_versions(connection, 1)[1]["softwareVersion"], "")
        self.assertEqual(version_text(b"\xff\xfe"), "")
        self.assertEqual(version_text("27.2\ninvalid"), "")
        connection.close()

    def test_standard_capabilities_use_service_uuid_not_name(self):
        for short, label in {
            "47": "Outlet", "45": "Lock", "82": "Humidity", "8A": "Temperature",
            "96": "Battery", "4A": "Thermostat", "CF": "Irrigation", "D9": "Input Source",
        }.items():
            self.assertEqual(service_capability(f"000000{short}-0000-1000-8000-0026BB765291"), label)
        self.assertEqual(service_capability("12345678-0000-1000-8000-0026BB765291"), "")

    def test_extractor_selects_primary_home_and_filters_lookups(self):
        connection = sqlite3.connect(":memory:")
        cursor = connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE ZMKFHOME (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT);
            CREATE TABLE ZMKFHOMEMANAGER (Z_PK INTEGER PRIMARY KEY, ZPRIMARYHOME INTEGER);
            CREATE TABLE ZMKFROOM (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZHOME INTEGER);
            CREATE TABLE ZMKFZONE (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZHOME INTEGER);
            CREATE TABLE Z_1ZONES (Z_1ROOMS INTEGER, Z_2ZONES INTEGER);
            CREATE TABLE ZMKFACCESSORY (
                Z_PK INTEGER PRIMARY KEY, ZCONFIGUREDNAME TEXT, ZPROVIDEDNAME TEXT,
                ZMANUFACTURER TEXT, ZMODEL TEXT, ZUNIQUEIDENTIFIER TEXT,
                ZROOM INTEGER, ZHOME INTEGER
            );
            CREATE TABLE ZMKFSERVICE (
                Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZACCESSORY INTEGER,
                ZEXPECTEDCONFIGUREDNAME TEXT, ZPROVIDEDNAME TEXT, ZMODELID BLOB
            );
            CREATE TABLE ZMKFCHARACTERISTIC (
                Z_PK INTEGER PRIMARY KEY, ZTYPE BLOB, ZSERVICE INTEGER,
                ZINSTANCEID INTEGER, ZMANUFACTURERDESCRIPTION TEXT, ZFORMAT TEXT
            );
            INSERT INTO ZMKFHOME VALUES (1, 'First Home'), (2, 'Primary Home');
            INSERT INTO ZMKFHOMEMANAGER VALUES (1, 2);
            INSERT INTO ZMKFROOM VALUES (11, 'First Room', 1), (21, 'Primary Room', 2);
            INSERT INTO ZMKFZONE VALUES (12, 'First Zone', 1), (22, 'Primary Zone', 2);
            INSERT INTO Z_1ZONES VALUES (11, 12), (21, 22);
            INSERT INTO ZMKFACCESSORY VALUES
                (13, 'First Accessory', NULL, NULL, NULL, 'first', 11, 1),
                (23, 'Primary Accessory', NULL, NULL, NULL, 'primary', 21, 2);
            INSERT INTO ZMKFSERVICE VALUES
                (14, 'First Service', 13, NULL, NULL, NULL),
                (24, 'Primary Service', 23, NULL, NULL, NULL);
            INSERT INTO ZMKFCHARACTERISTIC VALUES
                (15, NULL, 14, 1, 'First Characteristic', 'bool'),
                (25, NULL, 24, 1, 'Primary Characteristic', 'bool');
            """
        )

        home = _extract_home_selection(cursor)
        rooms = _build_room_lookup(cursor, home["id"])
        zones = _build_zone_lookup(cursor, rooms, home["id"])
        accessories = _build_accessory_lookup(cursor, rooms, home["id"])
        services = _build_service_lookup(cursor, accessories)
        characteristics, _ = _build_characteristic_lookup(cursor, services)

        self.assertEqual(home, {
            "id": 2,
            "name": "Primary Home",
            "selection": "primary",
            "availableCount": 2,
        })
        self.assertEqual(rooms, {21: "Primary Room"})
        self.assertEqual(zones, {"Primary Zone": ["Primary Room"]})
        self.assertEqual(set(accessories), {23})
        self.assertEqual(set(services), {24})
        self.assertEqual(set(characteristics), {25})
        connection.close()

    def test_generator_filters_layout_and_scenes_by_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "core.sqlite"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE ZMKFROOM (Z_PK INTEGER PRIMARY KEY, ZNAME TEXT);
                CREATE TABLE ZMKFACCESSORY (
                    Z_PK INTEGER PRIMARY KEY, ZCONFIGUREDNAME TEXT, ZPROVIDEDNAME TEXT,
                    ZMANUFACTURER TEXT, ZMODEL TEXT, ZUNIQUEIDENTIFIER TEXT,
                    ZROOM INTEGER, ZHOME INTEGER, ZMATTERNODEID INTEGER,
                    ZMATTERVENDORID INTEGER, ZMATTERPRODUCTID INTEGER,
                    ZHOSTACCESSORY INTEGER
                );
                CREATE TABLE ZMKFSERVICE (
                    Z_PK INTEGER PRIMARY KEY, ZEXPECTEDCONFIGUREDNAME TEXT,
                    ZNAME TEXT, ZPROVIDEDNAME TEXT, ZACCESSORY INTEGER,
                    ZSERVICETYPE BLOB
                );
                CREATE TABLE ZMKFACTIONSET (
                    Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZTYPE TEXT, ZHOME INTEGER
                );
                CREATE TABLE ZMKFACTION (
                    Z_PK INTEGER PRIMARY KEY, ZACTIONSET INTEGER, Z_ENT INTEGER,
                    ZTARGETVALUE BLOB, ZSTATE INTEGER, ZVOLUME INTEGER,
                    ZSERVICE INTEGER, ZACCESSORY1 INTEGER, ZCHARACTERISTICID INTEGER
                );
                CREATE TABLE ZMKFCHARACTERISTIC (
                    ZSERVICE INTEGER, ZINSTANCEID INTEGER,
                    ZMANUFACTURERDESCRIPTION TEXT, ZFORMAT TEXT
                );
                CREATE TABLE ZMKFRESIDENTSELECTION (
                    Z_PK INTEGER PRIMARY KEY, ZHOME INTEGER,
                    ZPREFERREDRESIDENTIDSIDENTIFIERS BLOB
                );
                CREATE TABLE ZMKFRESIDENT (
                    Z_PK INTEGER PRIMARY KEY, ZNAME TEXT, ZREACHABLE INTEGER,
                    ZIDSIDENTIFIER BLOB, ZAPPLEMEDIAACCESSORY INTEGER, ZHOME INTEGER
                );
                INSERT INTO ZMKFROOM VALUES (11, 'First Room'), (21, 'Primary Room');
                INSERT INTO ZMKFACCESSORY VALUES
                    (13, 'First Accessory', NULL, NULL, NULL, 'first', 11, 1, NULL, NULL, NULL, NULL),
                    (23, 'Primary Accessory', NULL, NULL, NULL, 'primary', 21, 2, NULL, NULL, NULL, NULL),
                    (30, 'Primary Bridge', NULL, NULL, NULL, 'bridge-2', 21, 2, NULL, NULL, NULL, 30),
                    (31, 'Primary Child', NULL, NULL, NULL, 'child-2', 21, 2, NULL, NULL, NULL, 30),
                    (40, 'First Bridge', NULL, NULL, NULL, 'bridge-1', 11, 1, NULL, NULL, NULL, 40),
                    (41, 'First Child', NULL, NULL, NULL, 'child-1', 11, 1, NULL, NULL, NULL, 40);
                INSERT INTO ZMKFSERVICE VALUES
                    (14, NULL, 'First Service', NULL, 13, NULL),
                    (24, NULL, 'Primary Service', NULL, 23, NULL),
                    (34, NULL, 'Primary Child Service', NULL, 31, NULL),
                    (44, NULL, 'First Child Service', NULL, 41, NULL);
                INSERT INTO ZMKFACTIONSET VALUES
                    (15, 'First Scene', 'HMActionSetTypeUserDefined', 1),
                    (25, 'Primary Scene', 'HMActionSetTypeUserDefined', 2);
                INSERT INTO ZMKFRESIDENTSELECTION VALUES (1, 1, X'01'), (2, 2, X'02');
                INSERT INTO ZMKFRESIDENT VALUES
                    (1, 'First Hub', 1, X'01', 13, 1),
                    (2, 'Primary Hub', 1, X'02', 23, 2);
                """
            )
            connection.executescript("""
                ALTER TABLE ZMKFACCESSORY ADD COLUMN ZSOFTWAREVERSION BLOB;
                ALTER TABLE ZMKFACCESSORY ADD COLUMN ZFIRMWAREVERSION TEXT;
                UPDATE ZMKFACCESSORY SET ZSOFTWAREVERSION = X'32372E32' WHERE Z_PK = 23;
                UPDATE ZMKFACCESSORY SET ZFIRMWAREVERSION = '3.0' WHERE Z_PK = 30;
                UPDATE ZMKFACCESSORY SET ZFIRMWAREVERSION = '1.2' WHERE Z_PK = 31;
                INSERT INTO ZMKFSERVICE VALUES
                    (50, NULL, 'Climate Channel', NULL, 31, X'000000820000100080000026BB765291'),
                    (51, NULL, NULL, NULL, 31, X'123456780000100080000026BB765291');
            """)
            connection.close()

            layout = load_home_layout(database, {"inventory": {"zones": []}}, 2)
            infrastructure = load_infrastructure(database, 2)
            scenes = load_scenes(database, 2)

        self.assertEqual(layout["stats"]["accessories"], 3)
        self.assertEqual(
            {item["name"] for item in layout["roomsWithoutZone"][0]["accessories"]},
            {"Primary Accessory", "Primary Bridge", "Primary Child"},
        )
        self.assertEqual([scene["name"] for scene in scenes], ["Primary Scene"])
        self.assertEqual([hub["name"] for hub in infrastructure["homeHubs"]], ["Primary Accessory"])
        self.assertEqual([bridge["name"] for bridge in infrastructure["bridges"]], ["Primary Bridge"])
        self.assertEqual(infrastructure["homeHubs"][0]["softwareVersion"], "27.2")
        self.assertTrue(infrastructure["homeHubs"][0]["primary"])
        self.assertEqual(infrastructure["bridges"][0]["firmwareVersion"], "3.0")
        self.assertEqual(infrastructure["bridges"][0]["accessories"][0]["firmwareVersion"], "1.2")
        child = next(a for a in layout["roomsWithoutZone"][0]["accessories"] if a["id"] == 31)
        self.assertIn("Humidity", child["capabilities"])
        self.assertTrue(any(s["originalName"] == "Climate Channel" for s in child["services"]))
        self.assertTrue(any(s["name"].startswith("Unclassified service") for s in child["services"]))


if __name__ == "__main__":
    unittest.main()
