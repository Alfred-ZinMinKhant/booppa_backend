async function main() {
  const signers = await ethers.getSigners();
  if (!signers || signers.length === 0) {
    console.error('No signers available. Ensure DEPLOY_PRIVATE_KEY and POLYGON_RPC_URL are set in ../.env or pass accounts in hardhat config.');
    process.exit(1);
  }

  const deployer = signers[0];
  const address = await deployer.getAddress?.() || deployer.address;
  console.log('Deployer address:', address);

  const balance = await deployer.getBalance();
  console.log('Balance (wei):', balance.toString());
  console.log('Balance (MATIC):', ethers.utils.formatEther(balance));
}

main()
  .then(() => process.exit(0))
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
