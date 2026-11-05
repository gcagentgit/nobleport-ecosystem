// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../interfaces/IIdentityRegistry.sol";
import "../interfaces/ICompliance.sol";

/**
 * NoblePort Property Registry — fractional real-estate interests as an
 * identity-gated ERC-1155 multi-token.
 *
 * One token id = one property (deed). Balances of that id are fractional
 * interests in the property's SPV. Every transfer and every issuance is
 * gated the same way as NBPTSecurityToken: the recipient must be verified
 * in the IdentityRegistry, and an optional ModularCompliance module can
 * veto. Nothing moves to an unverified wallet.
 *
 * Human-gated lifecycle (L3: two distinct signers):
 *   1. registerProperty   — an AGENT records the deed hash and parameters.
 *                            No tokens exist yet.
 *   2. attestProperty     — a VERIFIER (must differ from the registering
 *                            agent) confirms title/appraisal evidence.
 *   3. issueFractions     — an AGENT mints the full fraction supply to a
 *                            verified custody wallet. Allowed once, only
 *                            after attestation.
 *   4. updateValuation    — a VERIFIER records a new appraisal (event only).
 *   5. retireFractions    — an AGENT burns fractions on disposition.
 *
 * Status: STAGED. Tokenized fractional ownership is BLOCKED under
 * core/config/launch-gates.json until securities counsel clears it. This
 * contract is the framework; no deployment is authorized by this code.
 *
 * Dependency-free by design (same convention as the rest of contracts/src).
 */
contract PropertyRegistry {
    // ── ERC-165 / ERC-1155 interface ids ─────────────────────────
    bytes4 private constant _INTERFACE_ID_ERC165 = 0x01ffc9a7;
    bytes4 private constant _INTERFACE_ID_ERC1155 = 0xd9b67a26;
    bytes4 private constant _INTERFACE_ID_ERC1155_METADATA_URI = 0x0e89341c;
    bytes4 private constant _ERC1155_RECEIVED = 0xf23a6e61;
    bytes4 private constant _ERC1155_BATCH_RECEIVED = 0xbc197c81;

    struct Property {
        bytes32 deedHash;        // keccak256 of the legal deed identifier / document hash
        string legalDeedId;      // human-readable registry index (county book/page, APN, etc.)
        string metadataURI;      // off-chain record (IPFS/Arweave) — PII never on-chain
        uint256 totalFractions;  // fixed at registration
        uint256 valuationUSD;    // whole US dollars, latest attested appraisal
        address registeredBy;
        address attestedBy;
        uint64 registeredAt;
        uint64 attestedAt;
        uint64 issuedAt;
        bool attested;
        bool issued;
        bool frozen;
    }

    address public owner;
    IIdentityRegistry public identityRegistry;
    ICompliance public compliance; // optional: address(0) disables the module hook
    bool public paused;
    string private _baseURI;

    uint256 public propertyCount;
    mapping(uint256 => Property) private _properties;
    mapping(bytes32 => uint256) private _deedToId;

    mapping(address => bool) public agents;
    mapping(address => bool) public verifiers;
    mapping(address => bool) public frozenWallet;

    mapping(uint256 => mapping(address => uint256)) private _balances;
    mapping(address => mapping(address => bool)) private _operatorApprovals;

    // ── ERC-1155 events ──────────────────────────────────────────
    event TransferSingle(address indexed operator, address indexed from, address indexed to, uint256 id, uint256 value);
    event TransferBatch(address indexed operator, address indexed from, address indexed to, uint256[] ids, uint256[] values);
    event ApprovalForAll(address indexed account, address indexed operator, bool approved);
    event URI(string value, uint256 indexed id);

    // ── Registry events ──────────────────────────────────────────
    event PropertyRegistered(uint256 indexed propertyId, bytes32 indexed deedHash, string legalDeedId, uint256 totalFractions, uint256 valuationUSD, address indexed agent);
    event PropertyAttested(uint256 indexed propertyId, address indexed verifier, string evidenceURI);
    event FractionsIssued(uint256 indexed propertyId, address indexed custody, uint256 totalFractions, address indexed agent);
    event FractionsRetired(uint256 indexed propertyId, address indexed from, uint256 amount, address indexed agent);
    event ValuationUpdated(uint256 indexed propertyId, uint256 previousUSD, uint256 newUSD, address indexed verifier);
    event PropertyFrozen(uint256 indexed propertyId, bool frozen, address indexed agent);
    event WalletFrozen(address indexed wallet, bool frozen, address indexed agent);
    event RegistryPaused(address indexed agent);
    event RegistryUnpaused(address indexed agent);
    event AgentAdded(address indexed agent);
    event AgentRemoved(address indexed agent);
    event VerifierAdded(address indexed verifier);
    event VerifierRemoved(address indexed verifier);
    event IdentityRegistrySet(address indexed registry);
    event ComplianceSet(address indexed compliance);

    modifier onlyOwner() {
        require(msg.sender == owner, "only owner");
        _;
    }

    modifier onlyAgent() {
        require(agents[msg.sender], "only agent");
        _;
    }

    modifier onlyVerifier() {
        require(verifiers[msg.sender], "only verifier");
        _;
    }

    modifier whenNotPaused() {
        require(!paused, "registry paused");
        _;
    }

    modifier exists(uint256 propertyId) {
        require(propertyId != 0 && propertyId <= propertyCount, "unknown property");
        _;
    }

    constructor(address identityRegistry_, address compliance_, string memory baseURI_) {
        require(identityRegistry_ != address(0), "identity registry required");
        owner = msg.sender;
        agents[msg.sender] = true;
        identityRegistry = IIdentityRegistry(identityRegistry_);
        compliance = ICompliance(compliance_);
        _baseURI = baseURI_;
    }

    // ── Lifecycle: register → attest → issue ─────────────────────

    /// @notice Record a deed. Creates no tokens. `legalDeedId` must be unique.
    function registerProperty(
        string calldata legalDeedId,
        uint256 totalFractions,
        uint256 valuationUSD,
        string calldata metadataURI
    ) external onlyAgent whenNotPaused returns (uint256 propertyId) {
        require(bytes(legalDeedId).length > 0, "deed id required");
        require(totalFractions > 0, "fractions must exceed zero");
        bytes32 deedHash = keccak256(bytes(legalDeedId));
        require(_deedToId[deedHash] == 0, "property already registered");

        propertyId = ++propertyCount;
        Property storage p = _properties[propertyId];
        p.deedHash = deedHash;
        p.legalDeedId = legalDeedId;
        p.metadataURI = metadataURI;
        p.totalFractions = totalFractions;
        p.valuationUSD = valuationUSD;
        p.registeredBy = msg.sender;
        p.registeredAt = uint64(block.timestamp);
        _deedToId[deedHash] = propertyId;

        emit PropertyRegistered(propertyId, deedHash, legalDeedId, totalFractions, valuationUSD, msg.sender);
        if (bytes(metadataURI).length > 0) emit URI(metadataURI, propertyId);
    }

    /// @notice Second signer confirms title + appraisal evidence. Must not be the registering agent.
    function attestProperty(uint256 propertyId, string calldata evidenceURI) external onlyVerifier whenNotPaused exists(propertyId) {
        Property storage p = _properties[propertyId];
        require(!p.attested, "already attested");
        require(msg.sender != p.registeredBy, "verifier must differ from registrar");
        p.attested = true;
        p.attestedBy = msg.sender;
        p.attestedAt = uint64(block.timestamp);
        emit PropertyAttested(propertyId, msg.sender, evidenceURI);
    }

    /// @notice Mint the full fraction supply to a verified custody/escrow wallet. Once per property, after attestation.
    function issueFractions(uint256 propertyId, address custody) external onlyAgent whenNotPaused exists(propertyId) {
        Property storage p = _properties[propertyId];
        require(p.attested, "not attested");
        require(!p.issued, "already issued");
        require(!p.frozen, "property frozen");
        require(custody != address(0), "custody required");
        _checkRecipient(address(0), custody, propertyId, p.totalFractions);

        p.issued = true;
        p.issuedAt = uint64(block.timestamp);
        _balances[propertyId][custody] += p.totalFractions;

        if (address(compliance) != address(0)) compliance.created(custody, p.totalFractions);
        emit TransferSingle(msg.sender, address(0), custody, propertyId, p.totalFractions);
        emit FractionsIssued(propertyId, custody, p.totalFractions, msg.sender);
    }

    /// @notice Burn fractions on disposition / redemption. Agent-only, audited by event.
    function retireFractions(uint256 propertyId, address from, uint256 amount) external onlyAgent exists(propertyId) {
        require(_balances[propertyId][from] >= amount, "retire exceeds balance");
        _balances[propertyId][from] -= amount;
        if (address(compliance) != address(0)) compliance.destroyed(from, amount);
        emit TransferSingle(msg.sender, from, address(0), propertyId, amount);
        emit FractionsRetired(propertyId, from, amount, msg.sender);
    }

    function updateValuation(uint256 propertyId, uint256 newValuationUSD) external onlyVerifier exists(propertyId) {
        Property storage p = _properties[propertyId];
        uint256 previous = p.valuationUSD;
        p.valuationUSD = newValuationUSD;
        emit ValuationUpdated(propertyId, previous, newValuationUSD, msg.sender);
    }

    function setPropertyURI(uint256 propertyId, string calldata metadataURI) external onlyAgent exists(propertyId) {
        _properties[propertyId].metadataURI = metadataURI;
        emit URI(metadataURI, propertyId);
    }

    // ── Reads ────────────────────────────────────────────────────

    function getProperty(uint256 propertyId) external view exists(propertyId) returns (Property memory) {
        return _properties[propertyId];
    }

    function getPropertyByDeed(string calldata legalDeedId) external view returns (uint256 propertyId, Property memory property) {
        propertyId = _deedToId[keccak256(bytes(legalDeedId))];
        require(propertyId != 0, "deed not found");
        property = _properties[propertyId];
    }

    function propertyIdForDeedHash(bytes32 deedHash) external view returns (uint256) {
        return _deedToId[deedHash];
    }

    function uri(uint256 propertyId) external view exists(propertyId) returns (string memory) {
        string memory specific = _properties[propertyId].metadataURI;
        return bytes(specific).length > 0 ? specific : _baseURI;
    }

    function supportsInterface(bytes4 interfaceId) external pure returns (bool) {
        return interfaceId == _INTERFACE_ID_ERC165 || interfaceId == _INTERFACE_ID_ERC1155 || interfaceId == _INTERFACE_ID_ERC1155_METADATA_URI;
    }

    // ── ERC-1155 core ────────────────────────────────────────────

    function balanceOf(address account, uint256 id) public view returns (uint256) {
        require(account != address(0), "zero address");
        return _balances[id][account];
    }

    function balanceOfBatch(address[] calldata accounts, uint256[] calldata ids) external view returns (uint256[] memory batch) {
        require(accounts.length == ids.length, "length mismatch");
        batch = new uint256[](accounts.length);
        for (uint256 i; i < accounts.length; ++i) batch[i] = balanceOf(accounts[i], ids[i]);
    }

    function setApprovalForAll(address operator, bool approved) external {
        require(operator != msg.sender, "self approval");
        _operatorApprovals[msg.sender][operator] = approved;
        emit ApprovalForAll(msg.sender, operator, approved);
    }

    function isApprovedForAll(address account, address operator) public view returns (bool) {
        return _operatorApprovals[account][operator];
    }

    function safeTransferFrom(address from, address to, uint256 id, uint256 amount, bytes calldata data) external whenNotPaused {
        require(from == msg.sender || isApprovedForAll(from, msg.sender), "not owner nor approved");
        _transfer(from, to, id, amount);
        emit TransferSingle(msg.sender, from, to, id, amount);
        _checkOnReceived(msg.sender, from, to, id, amount, data);
    }

    function safeBatchTransferFrom(address from, address to, uint256[] calldata ids, uint256[] calldata amounts, bytes calldata data) external whenNotPaused {
        require(from == msg.sender || isApprovedForAll(from, msg.sender), "not owner nor approved");
        require(ids.length == amounts.length, "length mismatch");
        for (uint256 i; i < ids.length; ++i) _transfer(from, to, ids[i], amounts[i]);
        emit TransferBatch(msg.sender, from, to, ids, amounts);
        _checkOnBatchReceived(msg.sender, from, to, ids, amounts, data);
    }

    // ── ERC-3643-style gating ────────────────────────────────────

    function _transfer(address from, address to, uint256 id, uint256 amount) internal {
        require(id != 0 && id <= propertyCount, "unknown property");
        require(to != address(0), "transfer to zero");
        require(!_properties[id].frozen, "property frozen");
        require(!frozenWallet[from], "sender frozen");
        _checkRecipient(from, to, id, amount);
        uint256 bal = _balances[id][from];
        require(bal >= amount, "insufficient fractions");
        unchecked { _balances[id][from] = bal - amount; }
        _balances[id][to] += amount;
        if (address(compliance) != address(0)) compliance.transferred(from, to, amount);
    }

    function _checkRecipient(address from, address to, uint256, uint256 amount) internal view {
        require(!frozenWallet[to], "recipient frozen");
        require(identityRegistry.isVerified(to), "recipient not verified");
        if (address(compliance) != address(0)) require(compliance.canTransfer(from, to, amount), "compliance rejected");
    }

    function _checkOnReceived(address operator, address from, address to, uint256 id, uint256 amount, bytes calldata data) private {
        if (to.code.length == 0) return;
        (bool ok, bytes memory ret) = to.call(abi.encodeWithSelector(_ERC1155_RECEIVED, operator, from, id, amount, data));
        require(ok && ret.length >= 32 && abi.decode(ret, (bytes4)) == _ERC1155_RECEIVED, "receiver rejected");
    }

    function _checkOnBatchReceived(address operator, address from, address to, uint256[] calldata ids, uint256[] calldata amounts, bytes calldata data) private {
        if (to.code.length == 0) return;
        (bool ok, bytes memory ret) = to.call(abi.encodeWithSelector(_ERC1155_BATCH_RECEIVED, operator, from, ids, amounts, data));
        require(ok && ret.length >= 32 && abi.decode(ret, (bytes4)) == _ERC1155_BATCH_RECEIVED, "receiver rejected");
    }

    // ── Agent controls ───────────────────────────────────────────

    function setPropertyFrozen(uint256 propertyId, bool frozen) external onlyAgent exists(propertyId) {
        _properties[propertyId].frozen = frozen;
        emit PropertyFrozen(propertyId, frozen, msg.sender);
    }

    function setWalletFrozen(address wallet, bool frozen) external onlyAgent {
        frozenWallet[wallet] = frozen;
        emit WalletFrozen(wallet, frozen, msg.sender);
    }

    function pause() external onlyAgent {
        paused = true;
        emit RegistryPaused(msg.sender);
    }

    function unpause() external onlyAgent {
        paused = false;
        emit RegistryUnpaused(msg.sender);
    }

    // ── Owner admin ──────────────────────────────────────────────

    function addAgent(address agent) external onlyOwner {
        agents[agent] = true;
        emit AgentAdded(agent);
    }

    function removeAgent(address agent) external onlyOwner {
        agents[agent] = false;
        emit AgentRemoved(agent);
    }

    function addVerifier(address verifier) external onlyOwner {
        verifiers[verifier] = true;
        emit VerifierAdded(verifier);
    }

    function removeVerifier(address verifier) external onlyOwner {
        verifiers[verifier] = false;
        emit VerifierRemoved(verifier);
    }

    function setIdentityRegistry(address registry) external onlyOwner {
        require(registry != address(0), "identity registry required");
        identityRegistry = IIdentityRegistry(registry);
        emit IdentityRegistrySet(registry);
    }

    function setCompliance(address compliance_) external onlyOwner {
        compliance = ICompliance(compliance_);
        emit ComplianceSet(compliance_);
    }

    function setBaseURI(string calldata baseURI_) external onlyOwner {
        _baseURI = baseURI_;
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "zero owner");
        owner = newOwner;
    }
}
