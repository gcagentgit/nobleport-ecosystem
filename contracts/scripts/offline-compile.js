/**
 * Offline compile: builds Hardhat-format artifacts with solc-js so tests can
 * run where binaries.soliditylang.org is unreachable (CI sandboxes, air-gapped
 * review machines). Mirrors hardhat.config.js settings (0.8.24, optimizer 200, viaIR).
 *
 *   SOLC_MODULE=/path/to/node_modules/solc node scripts/offline-compile.js
 *   npx hardhat test --no-compile
 */
const fs = require("fs");
const path = require("path");
const solc = require(process.env.SOLC_MODULE || "solc");

const root = path.resolve(__dirname, "..");
const srcDir = path.join(root, "src");
const artifactsDir = path.join(root, "artifacts");

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = path.join(dir, e.name);
    return e.isDirectory() ? walk(p) : p.endsWith(".sol") ? [p] : [];
  });
}

const sources = {};
for (const file of walk(srcDir)) {
  const sourceName = path.relative(root, file).split(path.sep).join("/");
  sources[sourceName] = { content: fs.readFileSync(file, "utf8") };
}

const input = {
  language: "Solidity",
  sources,
  settings: {
    optimizer: { enabled: true, runs: 200 },
    viaIR: true,
    outputSelection: { "*": { "*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object"] } },
  },
};

function findImports(importPath) {
  // Imports are relative ("../interfaces/X.sol"); solc resolves them against the source name.
  const candidate = path.join(root, importPath);
  return fs.existsSync(candidate) ? { contents: fs.readFileSync(candidate, "utf8") } : { error: `not found: ${importPath}` };
}

const output = JSON.parse(solc.compile(JSON.stringify(input), { import: findImports }));
const errors = (output.errors || []).filter((e) => e.severity === "error");
for (const e of output.errors || []) console.error(e.formattedMessage);
if (errors.length) process.exit(1);

let count = 0;
for (const [sourceName, contracts] of Object.entries(output.contracts)) {
  for (const [contractName, c] of Object.entries(contracts)) {
    const dir = path.join(artifactsDir, sourceName);
    fs.mkdirSync(dir, { recursive: true });
    const artifact = {
      _format: "hh-sol-artifact-1",
      contractName,
      sourceName,
      abi: c.abi,
      bytecode: "0x" + c.evm.bytecode.object,
      deployedBytecode: "0x" + c.evm.deployedBytecode.object,
      linkReferences: {},
      deployedLinkReferences: {},
    };
    fs.writeFileSync(path.join(dir, `${contractName}.json`), JSON.stringify(artifact, null, 2));
    count++;
  }
}
console.log(`compiled ${count} contract(s) with solc ${solc.version()} → ${path.relative(root, artifactsDir)}/`);
