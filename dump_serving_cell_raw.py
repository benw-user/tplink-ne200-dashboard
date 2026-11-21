"""One-off diagnostic: dumps the FULL raw response for DEV2_LTE_SERVING_CELL_INFO
with NO attribute filter, instead of guessing attribute names one at a time.

Some embedded APIs return every available field when you don't restrict the
attrs list - if so, this reveals the real 4G SNR field name directly instead
of us guessing (a guess that's wrong can break the whole request, as seen
with the earlier SINR/SNR attempts). This is a single isolated request, not a
loop, so it carries minimal risk.

Usage:
    python dump_serving_cell_raw.py --host http://192.168.254.1 --username user
"""
from __future__ import annotations

import argparse
import os
from getpass import getpass
from logging import Logger, StreamHandler, WARNING

from tplinkrouterc6u import TplinkRouterProvider


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://192.168.254.1")
    parser.add_argument("--username", default="user")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    password = os.environ.get("TPLINK_PASSWORD") or getpass("Router password: ")

    logger = Logger("dump_serving_cell_raw")
    logger.addHandler(StreamHandler())
    logger.setLevel(WARNING)

    router = TplinkRouterProvider.get_client(
        args.host, password, args.username, logger, timeout=args.timeout
    )
    router.req.headers["Connection"] = "close"
    router.authorize()

    # warm up the same way dashboard.py does before touching LTE-related OIDs
    router.get_firmware()
    router.get_status()
    router.get_ipv4_status()

    print("=== DEV2_LTE_SERVING_CELL_INFO with NO attribute filter ===")
    try:
        response, values = router.req_act(
            [router.ActItem(router.ActItem.GL, "DEV2_LTE_SERVING_CELL_INFO", attrs=[])]
        )
        print("raw:", response)
        print("parsed:", values)
    except Exception as exc:
        print(f"failed: {exc}")

    router.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
