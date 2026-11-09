"""Collect social posts into Db2 (labels them, links products/stores, embeds them as VECTORs).

    python src/ingest_social.py reddit "Kestrel watch" --subreddit r/smartwatch
    python src/ingest_social.py x "Bazaar delivery"
    python src/ingest_social.py file exports/posts.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyst.config import settings  # noqa: E402
from analyst.db import Db2  # noqa: E402
from analyst.embeddings import Embedder  # noqa: E402
from analyst.social import SocialStore  # noqa: E402
from analyst.social_connectors import FILE_FIELDS, ConnectorError, fetch_reddit, fetch_x, load_file  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="source", required=True)
    r = sub.add_parser("reddit", help="Reddit search (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET)")
    r.add_argument("query")
    r.add_argument("--subreddit")
    r.add_argument("--limit", type=int, default=100)
    x = sub.add_parser("x", help="X recent search (X_BEARER_TOKEN)")
    x.add_argument("query")
    x.add_argument("--limit", type=int, default=100)
    f = sub.add_parser("file", help=f"CSV/JSON export with: {FILE_FIELDS}")
    f.add_argument("path")
    f.add_argument("--platform", default="OTHER")
    args = ap.parse_args()

    try:
        if args.source == "reddit":
            posts = fetch_reddit(args.query, args.subreddit, args.limit)
        elif args.source == "x":
            posts = fetch_x(args.query, args.limit)
        else:
            posts = load_file(args.path, args.platform)
    except ConnectorError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"collected {len(posts)} posts from {args.source}")
    store = SocialStore(Db2(), Embedder(settings), settings)
    n = store.ingest(posts)
    print(f"stored {n} new posts in {settings.data_schema}.SOCIAL_POSTS "
          f"(+ embeddings in {settings.app_schema}.SOCIAL_EMBEDDINGS)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
