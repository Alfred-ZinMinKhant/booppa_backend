const path = require('path');
require('dotenv').config({ path: path.resolve(__dirname, '../.env') });
require('@nomiclabs/hardhat-ethers');

const { POLYGON_RPC_URL, DEPLOY_PRIVATE_KEY } = process.env;

module.exports = {
  solidity: {
    compilers: [
      {
        version: '0.8.24',
        settings: {
          optimizer: { enabled: true, runs: 200 }
        }
      }
    ]
  },
  networks: {
    amoy: {
      url: POLYGON_RPC_URL || 'https://rpc-mumbai.maticvigil.com',
      accounts: DEPLOY_PRIVATE_KEY ? [DEPLOY_PRIVATE_KEY] : []
    },
    mainnet: {
      url: POLYGON_RPC_URL || 'https://polygon-rpc.com',
      accounts: DEPLOY_PRIVATE_KEY ? [DEPLOY_PRIVATE_KEY] : []
    }
  },
  paths: {
    sources: './contracts',
    cache: './cache',
    artifacts: './artifacts'
  }
};
