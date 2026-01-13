# Hardhat deploy helper for Booppa

This folder contains a minimal Hardhat setup to compile and deploy `EvidenceAnchorV3.sol` (the repo's contract) to Polygon networks.

Prerequisites
- Node.js 18+ and npm
- An RPC URL (Infura/Alchemy/QuickNode) and a deployer private key (test key for dev only)

Setup
1. cd into the hardhat folder

```bash
cd hardhat
npm install
```

2. Create a `.env` file (next to `hardhat.config.js`) with the values below:

```
POLYGON_RPC_URL=https://polygon-amoy.infura.io/v3/YOUR_PROJECT_ID
DEPLOY_PRIVATE_KEY=0xYOUR_PRIVATE_KEY
```

Replace with your Infura/Alchemy RPC and a private key that has funds on the chosen network.

Deploy

- Compile:

```bash
npm run compile
```

- Deploy to Amoy (testnet):

```bash
npm run deploy:amoy
```

- Deploy to Mainnet (if ready):

```bash
npm run deploy:mainnet
```

After deployment the script prints the deployed contract address — copy that value into your project's `.env` as `ANCHOR_CONTRACT_ADDRESS` and restart the backend.

Security
- Do NOT commit private keys or RPC project secrets. Use Secrets Manager or environment injection in CI/CD for production deployments.
