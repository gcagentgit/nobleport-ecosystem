const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("PropertyRegistry (identity-gated ERC-1155 fractions)", function () {
  const DEED = "ESSEX-MA-BK12345-PG678";
  const FRACTIONS = 10_000n;
  const VALUATION = 1_250_000n;

  let owner, agent, verifier, custody, investor, outsider;
  let identity, registry;

  beforeEach(async () => {
    [owner, agent, verifier, custody, investor, outsider] = await ethers.getSigners();
    identity = await (await ethers.getContractFactory("MockIdentityRegistry")).deploy();
    registry = await (await ethers.getContractFactory("PropertyRegistry")).deploy(
      await identity.getAddress(),
      ethers.ZeroAddress,
      "ipfs://base/"
    );
    await registry.addAgent(agent.address);
    await registry.addVerifier(verifier.address);
    await identity.setVerified(custody.address, true);
    await identity.setVerified(investor.address, true);
  });

  async function registerAndAttest() {
    await registry.connect(agent).registerProperty(DEED, FRACTIONS, VALUATION, "ipfs://prop/1");
    await registry.connect(verifier).attestProperty(1, "ipfs://evidence/1");
  }

  it("registers a deed with a unique hash and no tokens", async () => {
    await expect(registry.connect(agent).registerProperty(DEED, FRACTIONS, VALUATION, "ipfs://prop/1"))
      .to.emit(registry, "PropertyRegistered")
      .withArgs(1, ethers.keccak256(ethers.toUtf8Bytes(DEED)), DEED, FRACTIONS, VALUATION, agent.address);
    expect(await registry.propertyCount()).to.equal(1);
    const [id, p] = await registry.getPropertyByDeed(DEED);
    expect(id).to.equal(1);
    expect(p.attested).to.equal(false);
    expect(p.issued).to.equal(false);
    expect(await registry.balanceOf(custody.address, 1)).to.equal(0);
    expect(await registry.uri(1)).to.equal("ipfs://prop/1");
    await expect(registry.connect(agent).registerProperty(DEED, 5n, 1n, "")).to.be.revertedWith("property already registered");
    await expect(registry.connect(agent).registerProperty("X", 0n, 1n, "")).to.be.revertedWith("fractions must exceed zero");
    await expect(registry.connect(outsider).registerProperty("Y", 1n, 1n, "")).to.be.revertedWith("only agent");
  });

  it("requires a second, distinct signer to attest before issuance", async () => {
    await registry.connect(agent).registerProperty(DEED, FRACTIONS, VALUATION, "");
    await expect(registry.connect(agent).issueFractions(1, custody.address)).to.be.revertedWith("not attested");
    // Same person wearing both hats is rejected.
    await registry.addVerifier(agent.address);
    await expect(registry.connect(agent).attestProperty(1, "x")).to.be.revertedWith("verifier must differ from registrar");
    await expect(registry.connect(verifier).attestProperty(1, "ipfs://evidence/1"))
      .to.emit(registry, "PropertyAttested").withArgs(1, verifier.address, "ipfs://evidence/1");
    await expect(registry.connect(verifier).attestProperty(1, "x")).to.be.revertedWith("already attested");
  });

  it("issues the full supply once, only to a verified custody wallet", async () => {
    await registerAndAttest();
    await expect(registry.connect(agent).issueFractions(1, outsider.address)).to.be.revertedWith("recipient not verified");
    await expect(registry.connect(agent).issueFractions(1, custody.address))
      .to.emit(registry, "FractionsIssued").withArgs(1, custody.address, FRACTIONS, agent.address)
      .and.to.emit(registry, "TransferSingle").withArgs(agent.address, ethers.ZeroAddress, custody.address, 1, FRACTIONS);
    expect(await registry.balanceOf(custody.address, 1)).to.equal(FRACTIONS);
    await expect(registry.connect(agent).issueFractions(1, custody.address)).to.be.revertedWith("already issued");
  });

  it("gates every transfer on identity verification and freezes", async () => {
    await registerAndAttest();
    await registry.connect(agent).issueFractions(1, custody.address);

    await expect(registry.connect(custody).safeTransferFrom(custody.address, investor.address, 1, 250n, "0x"))
      .to.emit(registry, "TransferSingle").withArgs(custody.address, custody.address, investor.address, 1, 250n);
    expect(await registry.balanceOf(investor.address, 1)).to.equal(250n);

    await expect(registry.connect(custody).safeTransferFrom(custody.address, outsider.address, 1, 1n, "0x"))
      .to.be.revertedWith("recipient not verified");
    await expect(registry.connect(investor).safeTransferFrom(investor.address, custody.address, 1, 251n, "0x"))
      .to.be.revertedWith("insufficient fractions");
    await expect(registry.connect(outsider).safeTransferFrom(custody.address, investor.address, 1, 1n, "0x"))
      .to.be.revertedWith("not owner nor approved");

    await registry.connect(agent).setWalletFrozen(investor.address, true);
    await expect(registry.connect(investor).safeTransferFrom(investor.address, custody.address, 1, 1n, "0x")).to.be.revertedWith("sender frozen");
    await registry.connect(agent).setWalletFrozen(investor.address, false);

    await registry.connect(agent).setPropertyFrozen(1, true);
    await expect(registry.connect(custody).safeTransferFrom(custody.address, investor.address, 1, 1n, "0x")).to.be.revertedWith("property frozen");
    await registry.connect(agent).setPropertyFrozen(1, false);

    await registry.connect(agent).pause();
    await expect(registry.connect(custody).safeTransferFrom(custody.address, investor.address, 1, 1n, "0x")).to.be.revertedWith("registry paused");
    await registry.connect(agent).unpause();

    // Operators and batch transfers follow the same gate.
    await registry.connect(custody).setApprovalForAll(agent.address, true);
    await expect(registry.connect(agent).safeBatchTransferFrom(custody.address, investor.address, [1], [10n], "0x"))
      .to.emit(registry, "TransferBatch");
    expect(await registry.balanceOf(investor.address, 1)).to.equal(260n);
    const batch = await registry.balanceOfBatch([custody.address, investor.address], [1, 1]);
    expect(batch[0] + batch[1]).to.equal(FRACTIONS);
  });

  it("records appraisals and retirements as events; supports ERC-165 ids", async () => {
    await registerAndAttest();
    await registry.connect(agent).issueFractions(1, custody.address);
    await expect(registry.connect(verifier).updateValuation(1, 1_400_000n))
      .to.emit(registry, "ValuationUpdated").withArgs(1, VALUATION, 1_400_000n, verifier.address);
    await expect(registry.connect(agent).updateValuation(1, 1n)).to.be.revertedWith("only verifier");
    await expect(registry.connect(agent).retireFractions(1, custody.address, 100n))
      .to.emit(registry, "FractionsRetired").withArgs(1, custody.address, 100n, agent.address);
    expect(await registry.balanceOf(custody.address, 1)).to.equal(FRACTIONS - 100n);
    expect(await registry.supportsInterface("0xd9b67a26")).to.equal(true); // ERC-1155
    expect(await registry.supportsInterface("0x0e89341c")).to.equal(true); // metadata URI
    expect(await registry.supportsInterface("0xffffffff")).to.equal(false);
    await expect(registry.getProperty(99)).to.be.revertedWith("unknown property");
  });

  it("consults the compliance module when one is attached", async () => {
    const compliance = await (await ethers.getContractFactory("ModularCompliance")).deploy(await identity.getAddress());
    await compliance.setTokenAddress(await registry.getAddress());
    await registry.setCompliance(await compliance.getAddress());
    await registerAndAttest();
    await registry.connect(agent).issueFractions(1, custody.address);
    await compliance.setMaxHolders(1); // custody already counts as a holder
    await expect(registry.connect(custody).safeTransferFrom(custody.address, investor.address, 1, 1n, "0x"))
      .to.be.revertedWith("compliance rejected");
  });
});
