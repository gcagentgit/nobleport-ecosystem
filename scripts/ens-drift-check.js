/**
 * Read-only drift check: compares the on-chain records of nobleport.eth and
 * its subnames against scripts/ens-records.json, and prints the resolver
 * calldata that would bring the chain in line.
 *
 * It NEVER sends a transaction. Prepared calls are submitted by humans through
 * the Safe at the governance level printed next to each call (L3 = 2 signers,
 * L4 = 3-of-5), after an AuditBeacon entry.
 *
 * Usage:
 *   npm install ethers bs58
 *   export ETH_RPC_URL="https://ethereum-rpc.publicnode.com"   # any mainnet RPC
 *   node scripts/ens-drift-check.js            # all names
 *   node scripts/ens-drift-check.js pay        # one subname
 *   node scripts/ens-drift-check.js --json     # machine-readable
 */

const fs = require("fs");
const path = require("path");
const { ethers } = require("ethers");
const bs58 = require("bs58");

const MANIFEST = JSON.parse(fs.readFileSync(path.join(__dirname, "ens-records.json"), "utf8"));
const ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e";
const REGISTRY_ABI = ["function resolver(bytes32 node) view returns (address)"];
const RESOLVER_ABI = [
  "function addr(bytes32 node) view returns (address)",
  "function addr(bytes32 node, uint256 coinType) view returns (bytes)",
  "function text(bytes32 node, string key) view returns (string)",
  "function contenthash(bytes32 node) view returns (bytes)",
  "function setAddr(bytes32 node, address a)",
  "function setAddr(bytes32 node, uint256 coinType, bytes a)",
  "function setText(bytes32 node, string key, string value)",
  "function setContenthash(bytes32 node, bytes hash)",
  "function multicall(bytes[] data) returns (bytes[])",
];
const iface = new ethers.Interface(RESOLVER_ABI);

function desiredFor(entry, isRoot) {
  if (isRoot) {
    const r = MANIFEST.root_records;
    return {
      eth: r.addresses.eth.value, sol: r.addresses.sol.value, contenthash: r.contenthash.value, texts: r.texts,
      gov: { address: r.addresses.eth.governance, contenthash: r.contenthash.governance, text: r.texts_governance },
    };
  }
  const lvl = entry.governance;
  return {
    eth: entry.addresses?.eth, sol: entry.addresses?.sol, contenthash: entry.contenthash, texts: entry.texts || {},
    gov: { address: lvl === "L3" ? "L4" : lvl, contenthash: lvl === "L3" ? "L4" : lvl, text: lvl },
  };
}

function classify(onchain, desired) {
  if (desired === undefined) return onchain ? "unmanaged" : "match";
  if (desired === null) return onchain ? "stale" : "match";
  if (!onchain) return "missing";
  return String(onchain).toLowerCase() === String(desired).toLowerCase() ? "match" : "drift";
}

async function checkName(provider, name, desired) {
  const node = ethers.namehash(name);
  const registry = new ethers.Contract(ENS_REGISTRY, REGISTRY_ABI, provider);
  const resolverAddr = await registry.resolver(node);
  const out = { name, node, resolver: resolverAddr === ethers.ZeroAddress ? null : resolverAddr, records: [], calls: [] };
  if (!out.resolver) {
    out.note = "No resolver set. The name owner must set a PublicResolver at app.ens.domains before records can be written.";
    return out;
  }
  const resolver = new ethers.Contract(out.resolver, RESOLVER_ABI, provider);

  const eth = await resolver["addr(bytes32)"](node).catch(() => null);
  const ethOn = eth && eth !== ethers.ZeroAddress ? eth : null;
  push(out, "addr:eth", ethOn, desired.eth, desired.gov.address, () =>
    iface.encodeFunctionData("setAddr(bytes32,address)", [node, desired.eth || ethers.ZeroAddress]));

  let solOn = null;
  try {
    const raw = await resolver["addr(bytes32,uint256)"](node, 501);
    if (raw && raw !== "0x" && ethers.dataLength(raw) === 32) solOn = bs58.encode(Buffer.from(ethers.getBytes(raw)));
  } catch { /* resolver lacks multicoin */ }
  push(out, "addr:sol", solOn, desired.sol, desired.gov.address, () =>
    iface.encodeFunctionData("setAddr(bytes32,uint256,bytes)", [node, 501, desired.sol ? bs58.decode(desired.sol) : "0x"]));

  let chOn = null;
  try {
    const raw = await resolver.contenthash(node);
    if (raw && raw !== "0x" && !/^0x0*$/.test(raw)) chOn = raw;
  } catch { /* no contenthash support */ }
  // Manifest contenthash is a URI; only "must be unset" (null) is auto-preparable here.
  push(out, "contenthash", chOn, desired.contenthash, desired.gov.contenthash, () => {
    if (desired.contenthash) throw new Error("Encode the CID with the gateway (nobleport-gateway/server/ens/contenthash.ts) or app.ens.domains");
    return iface.encodeFunctionData("setContenthash", [node, "0x"]);
  });

  const keys = new Set([...Object.keys(desired.texts), "url", "description", "email", "avatar", "com.twitter", "com.github", "com.reddit"]);
  for (const key of [...keys].sort()) {
    const on = (await resolver.text(node, key).catch(() => "")) || null;
    push(out, `text:${key}`, on, desired.texts[key], desired.gov.text, () =>
      iface.encodeFunctionData("setText", [node, key, desired.texts[key] ?? ""]));
  }
  if (out.calls.length > 0) {
    out.multicall = { to: out.resolver, value: "0", data: iface.encodeFunctionData("multicall", [out.calls.map((c) => c.data)]) };
    out.requiredGovernance = out.calls.some((c) => c.governance === "L4") ? "L4" : "L3";
  }
  return out;
}

function push(out, key, onchain, desired, governance, encode) {
  const status = classify(onchain, desired);
  const rec = { key, onchain, desired: desired === undefined ? "(unmanaged)" : desired, status, governance };
  out.records.push(rec);
  if (status === "missing" || status === "drift" || status === "stale") {
    try { out.calls.push({ key, governance, data: encode() }); }
    catch (e) { rec.note = String(e.message || e); }
  }
}

async function main() {
  const rpc = process.env.ETH_RPC_URL || MANIFEST.gateways.rpc_pool[0];
  const args = process.argv.slice(2);
  const json = args.includes("--json");
  const only = args.find((a) => !a.startsWith("--"));
  const provider = new ethers.JsonRpcProvider(rpc);

  const targets = [{ name: MANIFEST.root, desired: desiredFor(null, true) }]
    .concat(MANIFEST.subnames.map((s) => ({ name: `${s.label}.${MANIFEST.root}`, desired: desiredFor(s, false) })))
    .filter((t) => !only || t.name === only || t.name === `${only}.${MANIFEST.root}`);

  const results = [];
  for (const t of targets) {
    try { results.push(await checkName(provider, t.name, t.desired)); }
    catch (e) { results.push({ name: t.name, error: String(e.message || e) }); }
  }

  if (json) { console.log(JSON.stringify({ rpc, checkedAt: new Date().toISOString(), results }, null, 2)); return; }

  console.log(`RPC: ${rpc}`);
  for (const r of results) {
    console.log(`\n== ${r.name}`);
    if (r.error) { console.log(`   ERROR: ${r.error}`); continue; }
    console.log(`   resolver: ${r.resolver || "(none)"}`);
    if (r.note) console.log(`   ${r.note}`);
    for (const rec of r.records) {
      if (rec.status === "match" || rec.status === "unmanaged") continue;
      console.log(`   ${rec.status.padEnd(8)} ${rec.key.padEnd(22)} on-chain=${rec.onchain ?? "—"}  declared=${rec.desired ?? "(clear)"}  [${rec.governance}]${rec.note ? "  " + rec.note : ""}`);
    }
    if (r.calls?.length) {
      console.log(`   → ${r.calls.length} call(s) prepared; submit multicall via Safe (${r.requiredGovernance}):`);
      console.log(`     to=${r.multicall.to}\n     data=${r.multicall.data}`);
    } else if (r.resolver) {
      console.log("   in sync");
    }
  }
  const actionable = results.reduce((n, r) => n + (r.calls?.length || 0), 0);
  console.log(`\n${actionable === 0 ? "STATUS: IN SYNC" : `STATUS: ${actionable} change(s) pending human approval`}`);
  process.exit(results.some((r) => r.error) ? 2 : 0);
}

main().catch((err) => { console.error(err); process.exit(1); });
