// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../interfaces/IIdentityRegistry.sol";

/// @dev Test double: a plain allow-list standing in for IdentityRegistry.
contract MockIdentityRegistry is IIdentityRegistry {
    mapping(address => bool) private _verified;
    mapping(address => uint16) private _country;

    function setVerified(address wallet, bool ok) external {
        _verified[wallet] = ok;
    }

    function isVerified(address wallet) external view override returns (bool) {
        return _verified[wallet];
    }

    function identity(address wallet) external pure override returns (address) {
        return wallet;
    }

    function investorCountry(address wallet) external view override returns (uint16) {
        return _country[wallet];
    }

    function registerIdentity(address wallet, address, uint16 country) external override {
        _verified[wallet] = true;
        _country[wallet] = country;
        emit IdentityRegistered(wallet, wallet);
    }

    function deleteIdentity(address wallet) external override {
        _verified[wallet] = false;
        emit IdentityRemoved(wallet, wallet);
    }

    function updateCountry(address wallet, uint16 country) external override {
        _country[wallet] = country;
        emit CountryUpdated(wallet, country);
    }
}
