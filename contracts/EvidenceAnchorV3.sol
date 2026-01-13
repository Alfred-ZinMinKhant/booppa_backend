// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title EvidenceAnchorV3
 * @dev Smart contract for anchoring evidence hashes on Polygon blockchain
 * @notice Provides immutable timestamping and verification for audit evidence
 */
contract EvidenceAnchorV3 {

    // Mapping of anchored hashes with timestamps
    mapping(bytes32 => uint256) public anchoredTimestamps;

    // Event emitted when a hash is anchored
    event HashAnchored(
        bytes32 indexed fileHash,
        address indexed anchoredBy,
        uint256 timestamp,
        string metadata
    );

    // Event emitted for batch anchoring
    event BatchAnchored(
        address indexed anchoredBy,
        uint256 count,
        uint256 timestamp
    );

    /**
     * @dev Anchor a single evidence hash
     * @param fileHash The SHA-256 hash of the evidence to anchor
     * @param metadata Additional metadata about the evidence
     */
    function anchorHash(bytes32 fileHash, string calldata metadata) external {
        require(fileHash != bytes32(0), "Invalid hash");
        require(anchoredTimestamps[fileHash] == 0, "Hash already anchored");

        anchoredTimestamps[fileHash] = block.timestamp;

        emit HashAnchored(
            fileHash,
            msg.sender,
            block.timestamp,
            metadata
        );
    }

    /**
     * @dev Anchor multiple evidence hashes in a single transaction
     * @param fileHashes Array of evidence hashes to anchor
     * @param metadata Array of metadata for each hash
     */
    function anchorBatch(
        bytes32[] calldata fileHashes,
        string[] calldata metadata
    ) external {
        require(fileHashes.length == metadata.length, "Array length mismatch");
        require(fileHashes.length > 0, "No hashes provided");
        require(fileHashes.length <= 100, "Too many hashes");

        for (uint256 i = 0; i < fileHashes.length; i++) {
            if (fileHashes[i] != bytes32(0) && anchoredTimestamps[fileHashes[i]] == 0) {
                anchoredTimestamps[fileHashes[i]] = block.timestamp;

                emit HashAnchored(
                    fileHashes[i],
                    msg.sender,
                    block.timestamp,
                    metadata[i]
                );
            }
        }

        emit BatchAnchored(msg.sender, fileHashes.length, block.timestamp);
    }

    /**
     * @dev Check if a hash is anchored and get its timestamp
     * @param fileHash The hash to check
     * @return isAnchored True if the hash is anchored
     * @return timestamp When the hash was anchored (0 if not anchored)
     */
    function isAnchored(bytes32 fileHash) external view returns (bool, uint256) {
        uint256 timestamp = anchoredTimestamps[fileHash];
        return (timestamp > 0, timestamp);
    }

    /**
     * @dev Verify the integrity of anchored data
     * @param fileHash The hash to verify
     * @param expectedTimestamp Expected anchoring timestamp
     * @return isValid True if the hash is anchored at the expected time
     */
    function verifyIntegrity(
        bytes32 fileHash,
        uint256 expectedTimestamp
    ) external view returns (bool) {
        return anchoredTimestamps[fileHash] == expectedTimestamp;
    }
}
