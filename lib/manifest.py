"""
Manifest creation, validation, and aggregation.

Each build phase produces a JSON manifest that captures:
  - What zcashd version was used
  - What addresses were created
  - What transactions were performed (TXIDs, heights, amounts, fees)
  - Wallet balances at the end of the phase
  - Viewing keys for read-only wallet testing
  - Chain state metadata

The per-phase manifests are aggregated into a full_manifest.json that
provides a complete picture of the wallet state across all NUs.

Manifest files are written to /artifacts/manifests/ and are the primary
deliverable alongside wallet.dat for downstream wallet tool testing.
"""

import json
import logging
import os
from datetime import datetime, timezone

from lib.constants import (
    Pool,
    NetworkUpgrade,
    NETWORK_UPGRADES,
    MANIFESTS_DIR,
    FINAL_DIR,
    get_all_pools_at_nu,
)
from lib.rpc_client import ZcashRPC
from lib.addresses import GeneratedAddresses
from lib.operations import TransactionRecord

logger = logging.getLogger(__name__)


def collect_balance_snapshot(rpc: ZcashRPC, phase_index: int) -> dict:
    """
    Collect a snapshot of wallet balances at the current chain state.

    Uses z_gettotalbalance for legacy versions (which splits into
    "transparent" and "private") and supplements with z_listunspent
    for per-pool breakdowns where available.

    Args:
        rpc:          RPC client
        phase_index:  Current phase (determines which RPCs are available)

    Returns:
        Dict with balance per pool and note/UTXO counts.
    """
    balances = {
        "transparent": "0",
        "sprout": "0",
        "sapling": "0",
        "orchard": "0",
    }
    note_counts = {"sprout": 0, "sapling": 0, "orchard": 0}
    utxo_count = 0

    # Get transparent UTXOs
    try:
        utxos = rpc.listunspent(1)
        utxo_count = len(utxos)
        t_balance = sum(u.get("amount", 0) for u in utxos)
        balances["transparent"] = f"{t_balance:.8f}"
    except Exception as e:
        logger.warning("Failed to list transparent UTXOs: %s", e)

    # Get shielded notes
    try:
        notes = rpc.z_listunspent(1)
        for note in notes:
            pool = note.get("type", note.get("pool", "unknown"))
            amount = note.get("amount", 0)

            if pool == "sprout":
                note_counts["sprout"] += 1
                balances["sprout"] = f"{float(balances['sprout']) + amount:.8f}"
            elif pool == "sapling":
                note_counts["sapling"] += 1
                balances["sapling"] = f"{float(balances['sapling']) + amount:.8f}"
            elif pool == "orchard":
                note_counts["orchard"] += 1
                balances["orchard"] = f"{float(balances['orchard']) + amount:.8f}"
    except Exception as e:
        logger.warning("Failed to list shielded notes: %s", e)

    # Count coinbase UTXOs specifically (for tracking unspent coinbase per era)
    coinbase_count = 0
    try:
        for utxo in utxos:
            if utxo.get("generated", False):
                coinbase_count += 1
    except Exception:
        pass

    return {
        "balances": balances,
        "note_counts": note_counts,
        "utxo_count": utxo_count,
        "coinbase_utxo_count": coinbase_count,
    }


def collect_chain_info(rpc: ZcashRPC) -> dict:
    """Collect chain state metadata from zcashd."""
    try:
        info = rpc.getblockchaininfo()
        return {
            "blocks": info.get("blocks", 0),
            "bestblockhash": info.get("bestblockhash", ""),
            "chain": info.get("chain", ""),
            "size_on_disk": info.get("size_on_disk", 0),
        }
    except Exception as e:
        logger.warning("Failed to get blockchain info: %s", e)
        return {}


def get_zcashd_commit(rpc: ZcashRPC) -> str:
    """
    Extract the git commit hash from the running zcashd's version info.

    The subversion string typically looks like:
      /MagicBean:2.0.7-2/
    or includes a commit hash in some versions.
    """
    try:
        info = rpc.call("getnetworkinfo")
        return info.get("subversion", "unknown")
    except Exception:
        return "unknown"


def create_phase_manifest(phase_index: int,
                          rpc: ZcashRPC,
                          addrs: GeneratedAddresses,
                          external_addrs: GeneratedAddresses,
                          viewing_keys: dict,
                          transactions: list[TransactionRecord]) -> dict:
    """
    Create a complete manifest for a single build phase.

    This captures everything needed to understand the wallet state at the
    end of this phase: what was created, what transactions occurred, what
    the balances are.

    Args:
        phase_index:    Build phase number
        rpc:            RPC client (for balance/chain queries)
        addrs:          Addresses generated in this phase
        external_addrs: External addresses used in this phase
        viewing_keys:   Exported viewing keys
        transactions:   All transaction records from this phase

    Returns:
        Complete manifest dict.
    """
    nu = NETWORK_UPGRADES[phase_index]

    # Collect live data from zcashd
    snapshot = collect_balance_snapshot(rpc, phase_index)
    chain_info = collect_chain_info(rpc)
    zcashd_commit = get_zcashd_commit(rpc)

    # Build nuparams map for this phase
    nuparams = {}
    for prev_nu in NETWORK_UPGRADES[:phase_index + 1]:
        if prev_nu.branch_id:
            nuparams[prev_nu.branch_id] = prev_nu.activation_height

    # Determine the phase end height: either the next NU's activation - 1,
    # or the current chain height for the last phase.
    if phase_index < len(NETWORK_UPGRADES) - 1:
        phase_end = NETWORK_UPGRADES[phase_index + 1].activation_height - 1
    else:
        phase_end = chain_info.get("blocks", 0)

    manifest = {
        "phase": phase_index,
        "network_upgrade": nu.id,
        "network_upgrade_name": nu.name,
        "zcashd_version": nu.zcashd_version,
        "zcashd_commit": zcashd_commit,
        "activation_height": nu.activation_height,
        "phase_start_height": nu.activation_height,
        "phase_end_height": phase_end,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "nuparams": nuparams,

        "addresses": addrs.to_dict(),
        "external_addresses": external_addrs.to_dict() if external_addrs else {},
        "viewing_keys": viewing_keys,

        "transactions": [tx.to_dict() for tx in transactions],

        "balances_at_phase_end": snapshot["balances"],
        "note_counts": snapshot["note_counts"],
        "utxo_count": snapshot["utxo_count"],
        "coinbase_utxo_count": snapshot["coinbase_utxo_count"],

        "chain_info": chain_info,

        "notes": nu.notes,
    }

    return manifest


def write_phase_manifest(manifest: dict, phase_index: int) -> str:
    """
    Write a phase manifest to the artifacts directory.

    Args:
        manifest:     The manifest dict
        phase_index:  Build phase number

    Returns:
        Path to the written manifest file.
    """
    nu = NETWORK_UPGRADES[phase_index]
    os.makedirs(MANIFESTS_DIR, exist_ok=True)

    filename = f"phase_{phase_index:02d}_{nu.id}.json"
    filepath = os.path.join(MANIFESTS_DIR, filename)

    with open(filepath, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=False)

    logger.info("Wrote phase manifest: %s", filepath)
    return filepath


def create_full_manifest(phase_manifests: list[dict]) -> dict:
    """
    Aggregate per-phase manifests into a single full manifest.

    The full manifest includes:
      - Summary of all phases
      - Cumulative transaction count and types
      - Final wallet state
      - Cross-reference of all addresses, viewing keys, and transactions
    """
    total_txs = sum(len(m.get("transactions", [])) for m in phase_manifests)
    final_phase = phase_manifests[-1] if phase_manifests else {}

    # Collect all unique addresses across all phases
    all_addresses = {
        "transparent": set(),
        "sprout": set(),
        "sapling": set(),
        "orchard": set(),
    }
    for m in phase_manifests:
        for pool, addr_list in m.get("addresses", {}).items():
            if pool == "unified":
                continue
            for entry in addr_list:
                addr = entry.get("address", entry) if isinstance(entry, dict) else entry
                if pool in all_addresses:
                    all_addresses[pool].add(addr)

    # Collect all viewing keys
    all_viewing_keys = {"sprout": [], "sapling": [], "orchard": []}
    seen_vks = set()
    for m in phase_manifests:
        for pool, vk_list in m.get("viewing_keys", {}).items():
            for vk_entry in vk_list:
                key = vk_entry.get("viewing_key", "")
                if key and key not in seen_vks:
                    seen_vks.add(key)
                    all_viewing_keys.setdefault(pool, []).append(vk_entry)

    full = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_phases": len(phase_manifests),
        "total_transactions": total_txs,
        "final_chain_height": final_phase.get("chain_info", {}).get("blocks", 0),

        "phases": [
            {
                "phase": m["phase"],
                "network_upgrade": m["network_upgrade_name"],
                "zcashd_version": m["zcashd_version"],
                "activation_height": m["activation_height"],
                "transaction_count": len(m.get("transactions", [])),
                "balances_at_end": m.get("balances_at_phase_end", {}),
            }
            for m in phase_manifests
        ],

        "all_addresses": {
            pool: sorted(addrs)
            for pool, addrs in all_addresses.items()
        },

        "all_viewing_keys": all_viewing_keys,

        "final_balances": final_phase.get("balances_at_phase_end", {}),
        "final_note_counts": final_phase.get("note_counts", {}),
        "final_utxo_count": final_phase.get("utxo_count", 0),
        "final_coinbase_utxo_count": final_phase.get("coinbase_utxo_count", 0),
    }

    return full


def write_full_manifest(phase_manifests: list[dict]) -> str:
    """
    Create and write the aggregated full manifest.

    Returns:
        Path to the written full_manifest.json.
    """
    full = create_full_manifest(phase_manifests)

    os.makedirs(FINAL_DIR, exist_ok=True)
    filepath = os.path.join(FINAL_DIR, "full_manifest.json")

    with open(filepath, "w") as f:
        json.dump(full, f, indent=2, sort_keys=False)

    logger.info("Wrote full manifest: %s", filepath)
    return filepath
