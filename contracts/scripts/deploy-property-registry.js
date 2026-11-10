/**
 * Deploys PropertyRegistry wired to an existing IdentityRegistry (and
 * optionally ModularCompliance). Local/testnet only: tokenized fractional
 * ownership is BLOCKED under core/config/launch-gates.json until counsel
 * clears it, so there is intentionally no mainnet script.
 *
 *   IDENTITY_REGISTRY=0x… [COMPLIANCE=0x…] [BASE_URI=ipfs://…] \
 *     npx hardhat run scripts/deploy-property-registry.js --network sepolia
 */
const hre = require("hardhat");

async function main() {
  if (hre.network.name === "mainnet") throw new Error("PropertyRegistry mainnet deployment is blocked (launch-gates: tokenized_fractional_ownership)");
  const [deployer] = await hre.ethers.getSigners();
  const identityRegistry = process.env.IDENTITY_REGISTRY;
  if (!identityRegistry) throw new Error("IDENTITY_REGISTRY is required");
  const compliance = process.env.COMPLIANCE || hre.ethers.ZeroAddress;
  const baseUri = process.env.BASE_URI || "ipfs://nobleport-property-metadata/";

  console.log("Deployer:", deployer.address, "network:", hre.network.name);
  const Registry = await hre.ethers.getContractFactory("PropertyRegistry");
  const registry = await Registry.deploy(identityRegistry, compliance, baseUri);
  await registry.waitForDeployment();
  console.log("PropertyRegistry:", await registry.getAddress());
  console.log("Next: addVerifier(<second signer>) via the owner multisig; register → attest → issue.");
}

main().catch((err) => { console.error(err); process.exit(1); });
