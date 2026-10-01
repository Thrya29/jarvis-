"""Build tests/data/geo-test.mmdb, a tiny DB-IP-shaped country database for tests.

Run once with:  uv run --with mmdb-writer --with netaddr python tests/data/make_geo_test_db.py
"""

from pathlib import Path

from mmdb_writer import MMDBWriter
from netaddr import IPSet

writer = MMDBWriter(ip_version=6, database_type="DBIP-Country-Lite", ipv4_compatible=True)
writer.insert_network(IPSet(["8.8.8.0/24"]), {"country": {"iso_code": "US"}})
writer.insert_network(IPSet(["49.36.0.0/16"]), {"country": {"iso_code": "IN"}})
writer.to_db_file(str(Path(__file__).with_name("geo-test.mmdb")))
