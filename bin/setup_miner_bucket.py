#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""GenTRX miner bucket setup — create S3 bucket and commit read credentials on-chain.

Supports Cloudflare R2 (production), Storj, Hippius, and MinIO (local dev).
Select the provider with --provider or GENTRX_AGENT_S3_PROVIDER.

Usage (R2) — credentials via env vars (preferred, keeps secrets out of ps aux):
    export GENTRX_AGENT_S3_ACCESS_KEY=<FULL_ACCESS_KEY>
    export GENTRX_AGENT_S3_SECRET_KEY=<FULL_SECRET_KEY>
    export GENTRX_AGENT_S3_READ_ACCESS_KEY=<READ_ONLY_KEY>
    export GENTRX_AGENT_S3_READ_SECRET_KEY=<READ_ONLY_SECRET>
    python bin/setup_miner_bucket \
        --provider r2 \
        --account-id <CF_ACCOUNT_ID> \
        --wallet-name miner \
        --wallet-hotkey default \
        --netuid 79

Usage (Storj):
    export GENTRX_AGENT_S3_ACCESS_KEY=<FULL_ACCESS_KEY>   # write grant
    export GENTRX_AGENT_S3_SECRET_KEY=<FULL_SECRET>
    export GENTRX_AGENT_S3_READ_ACCESS_KEY=<READ_ONLY_KEY>  # GetObject scope
    export GENTRX_AGENT_S3_READ_SECRET_KEY=<READ_ONLY_SECRET>
    python bin/setup_miner_bucket \
        --provider storj \
        --bucket <your-bucket-name> \
        --wallet-name miner --wallet-hotkey default --netuid 79
    # Endpoint defaults to https://gateway.storjshare.io, region "global".
    # The read grant only needs GetObject; ListBucket is not required for the
    # validator read flow (deterministic key paths).

Usage (R2) — credentials via flags (legacy; visible in ps aux):
    python bin/setup_miner_bucket \
        --account-id <CF_ACCOUNT_ID> \
        --access-key <FULL_ACCESS_KEY> \
        --secret-key <FULL_SECRET_KEY> \
        --read-access-key <READ_ONLY_KEY> \
        --read-secret-key <READ_ONLY_SECRET> \
        --wallet-name miner \
        --wallet-hotkey default \
        --netuid 79

Usage (local MinIO for testing):
    python bin/setup_miner_bucket \
        --endpoint http://localhost:9000 \
        --bucket agent-0 \
        --access-key minioadmin \
        --secret-key minioadmin \
        --read-access-key minioadmin \
        --read-secret-key minioadmin \
        --dry-run

What this does:
  1. Creates the S3 bucket if it doesn't exist
  2. Verifies write access (PUT test object)
  3. Verifies read access with read-only credentials (GET test object)
  4. Commits read credentials on-chain via bittensor Commitments pallet
     (or prints the commitment string for --dry-run)

On-chain commitment format (128 chars):
  [account_id: 32][access_key_id: 32][secret_access_key: 64]
  account_id:
    R2:      32-char Cloudflare account ID (hex)
    Storj:   "storj:<bucket-name>" sigil
    Hippius: bucket name
    MinIO:   bucket name
  access_key_id / secret_access_key = READ-ONLY credentials

Validators read this to discover and pull gradients from your bucket.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Set up GenTRX miner S3 bucket and commit credentials on-chain"
    )

    # Bucket location
    parser.add_argument(
        "--provider",
        default=os.environ.get("GENTRX_AGENT_S3_PROVIDER", ""),
        choices=("", "r2", "storj", "hippius", "minio"),
        help="Storage provider. Sets default endpoint/region and the "
        "account_id slot encoding on chain. Or set GENTRX_AGENT_S3_PROVIDER.",
    )
    parser.add_argument(
        "--account-id",
        default="",
        help="Cloudflare account ID (R2 only). Leave blank for other providers.",
    )
    parser.add_argument(
        "--endpoint",
        default="",
        help="Override S3 endpoint URL. If omitted, derived from --provider.",
    )
    parser.add_argument(
        "--bucket",
        default="",
        help="Bucket name. Required for Storj / Hippius / MinIO. "
        "For R2 defaults to --account-id.",
    )
    parser.add_argument("--region", default="")

    # Full-access credentials (for bucket creation and write verification)
    # Prefer env vars GENTRX_AGENT_S3_ACCESS_KEY / GENTRX_AGENT_S3_SECRET_KEY to
    # keep secrets out of the process list (ps aux).  Falls back to interactive prompt.
    parser.add_argument("--access-key", default=None, help="Full-access S3 access key (or GENTRX_AGENT_S3_ACCESS_KEY)")
    parser.add_argument("--secret-key", default=None, help="Full-access S3 secret key (or GENTRX_AGENT_S3_SECRET_KEY)")

    # Read-only credentials (committed on-chain for validator)
    parser.add_argument(
        "--read-access-key",
        default=None,
        help="Read-only S3 access key (committed on-chain; or GENTRX_AGENT_S3_READ_ACCESS_KEY)",
    )
    parser.add_argument(
        "--read-secret-key",
        default=None,
        help="Read-only S3 secret key (committed on-chain; or GENTRX_AGENT_S3_READ_SECRET_KEY)",
    )

    # Bittensor
    parser.add_argument("--wallet-name", default="default")
    parser.add_argument("--wallet-hotkey", default="default")
    parser.add_argument("--wallet-path", default="", help="Path to wallets dir (default: ~/.bittensor/wallets)")
    parser.add_argument("--netuid", type=int, default=79)
    parser.add_argument(
        "--subtensor-network", default="finney", help="finney | test | local"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip chain commitment — print commitment string only",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip bucket write/read verification",
    )

    args = parser.parse_args()

    # Resolve credentials: flag → env var → interactive prompt
    def _cred(flag_val, env_var, label, secret=False):
        if flag_val:
            return flag_val
        env_val = os.environ.get(env_var, "")
        if env_val:
            return env_val
        if secret:
            val = getpass.getpass(f"  {label}: ")
        else:
            val = input(f"  {label}: ").strip()
        if not val:
            print(f"ERROR: {label} is required.", file=sys.stderr)
            sys.exit(1)
        return val

    access_key = _cred(args.access_key, "GENTRX_AGENT_S3_ACCESS_KEY", "Full-access key ID")
    secret_key = _cred(args.secret_key, "GENTRX_AGENT_S3_SECRET_KEY", "Full-access secret key", secret=True)
    read_access_key = _cred(args.read_access_key, "GENTRX_AGENT_S3_READ_ACCESS_KEY", "Read-only key ID")
    read_secret_key = _cred(args.read_secret_key, "GENTRX_AGENT_S3_READ_SECRET_KEY", "Read-only secret key", secret=True)

    from GenTRX.src.chain import (
        ACCESS_KEY_LEN,
        ACCOUNT_ID_LEN,
        HIPPIUS_ENDPOINT,
        SECRET_KEY_LEN,
        STORJ_ENDPOINT,
        STORJ_PREFIX,
        BucketInfo,
    )

    provider = (args.provider or "").lower()
    if not provider and args.account_id:
        provider = "r2"

    account_id = args.account_id
    endpoint = args.endpoint
    region = args.region
    bucket = args.bucket

    if provider == "r2":
        if not account_id:
            print("ERROR: --account-id is required for --provider r2", file=sys.stderr)
            sys.exit(1)
        endpoint = endpoint or f"https://{account_id}.r2.cloudflarestorage.com"
        region = region or "auto"
        bucket = bucket or account_id
        commitment_account_id = account_id
    elif provider == "storj":
        if not bucket:
            print("ERROR: --bucket is required for --provider storj", file=sys.stderr)
            sys.exit(1)
        endpoint = endpoint or STORJ_ENDPOINT
        region = region or "global"
        commitment_account_id = f"{STORJ_PREFIX}{bucket}"
    elif provider == "hippius":
        if not bucket:
            print("ERROR: --bucket is required for --provider hippius", file=sys.stderr)
            sys.exit(1)
        endpoint = endpoint or HIPPIUS_ENDPOINT
        region = region or "decentralized"
        commitment_account_id = bucket
    else:
        # MinIO / unspecified — require explicit endpoint and bucket
        if not endpoint:
            print("ERROR: --endpoint is required when --provider is not set", file=sys.stderr)
            sys.exit(1)
        if not bucket:
            print("ERROR: --bucket is required when --provider is not set", file=sys.stderr)
            sys.exit(1)
        region = region or "auto"
        commitment_account_id = account_id or bucket

    # Length sanity check — warn before the overflow guard hard-fails
    def _warn_if_tight(name: str, value: str, slot: int) -> None:
        if len(value) > slot * 0.8:
            print(
                f"   WARNING: {name} length {len(value)} is >80% of slot {slot}",
                file=sys.stderr,
            )

    _warn_if_tight("account_id", commitment_account_id, ACCOUNT_ID_LEN)
    _warn_if_tight("access_key_id", read_access_key, ACCESS_KEY_LEN)
    _warn_if_tight("secret_access_key", read_secret_key, SECRET_KEY_LEN)

    print("\nGenTRX Miner Bucket Setup")
    print(f"  Provider : {provider or 'unspecified'}")
    print(f"  Endpoint : {endpoint}")
    print(f"  Bucket   : {bucket}")
    print(f"  Region   : {region}")
    print(f"  Dry run  : {args.dry_run}")
    print()

    # --- Step 1: Create bucket ---
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError

    boto_config = BotoConfig(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=10,
        read_timeout=15,
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=boto_config,
    )

    print("1. Creating bucket...")
    try:
        client.create_bucket(Bucket=bucket)
        print(f"   Created: {bucket}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            print(f"   Already exists: {bucket}")
        else:
            print(f"   ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    if not args.skip_verify:
        # --- Step 2: Write test object ---
        print("2. Verifying write access...")
        test_key = "gentrx/.setup_test"
        test_data = b"gentrx-setup-ok"
        try:
            client.put_object(Bucket=bucket, Key=test_key, Body=test_data)
            print("   Write: OK")
        except ClientError as e:
            print(f"   Write FAILED: {e}", file=sys.stderr)
            sys.exit(1)

        # --- Step 3: Verify read-only credentials ---
        print("3. Verifying read-only credentials...")
        read_client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=read_access_key,
            aws_secret_access_key=read_secret_key,
            region_name=region,
            config=boto_config,
        )
        try:
            resp = read_client.get_object(Bucket=bucket, Key=test_key)
            data = resp["Body"].read()
            assert data == test_data, f"Data mismatch: {data!r}"
            print("   Read:  OK")
        except ClientError as e:
            print(f"   Read FAILED: {e}", file=sys.stderr)
            print(
                "   Check that your read-only key has GET permission on this bucket.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Clean up test object
        client.delete_object(Bucket=bucket, Key=test_key)

    # --- Step 4: Build commitment string ---
    bucket_info = BucketInfo(
        account_id=commitment_account_id,
        access_key_id=read_access_key,
        secret_access_key=read_secret_key,
    )
    commitment = bucket_info.to_commitment()

    print(f"\n4. Commitment string ({len(commitment)} chars):")
    print(f"   {commitment}")

    if args.dry_run:
        print("\n[dry-run] Skipping chain commitment.")
        print(
            "To commit manually, pass this string to subtensor.commit(wallet, netuid, data)."
        )
        return

    # --- Step 5: Commit on-chain ---
    print(f"\n5. Committing to chain (netuid={args.netuid})...")
    try:
        import bittensor as bt

        # bittensor v9: Wallet/Subtensor are CapWords
        wallet_kwargs = dict(name=args.wallet_name, hotkey=args.wallet_hotkey)
        if args.wallet_path:
            wallet_kwargs["path"] = args.wallet_path
        wallet = bt.Wallet(**wallet_kwargs)
        subtensor = bt.Subtensor(network=args.subtensor_network)
        metagraph = subtensor.metagraph(args.netuid)

        from GenTRX.src.chain import GenTRXChain

        chain = GenTRXChain(subtensor, args.netuid, metagraph)
        chain.commit_bucket(wallet, bucket_info)
        print(
            "   Committed. Validator will discover your bucket on the next metagraph sync."
        )
    except ImportError:
        print("   bittensor not installed — commitment skipped.", file=sys.stderr)
        print("   Install: pip install bittensor", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"   Commitment FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    print("\nDone. Your bucket is ready for GenTRX gradient uploads.")
    print("  Set in your miner env:")
    print(f"    GENTRX_AGENT_S3_BUCKET={bucket}")
    print(f"    GENTRX_AGENT_S3_ENDPOINT_URL={endpoint}")
    print("    GENTRX_AGENT_S3_ACCESS_KEY=<your write access key>")
    print("    GENTRX_AGENT_S3_SECRET_KEY=<your write secret key>")


if __name__ == "__main__":
    main()
