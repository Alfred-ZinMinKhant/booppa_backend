async function main() {
  const [deployer] = await ethers.getSigners();
  console.log('Deploying contracts with the account:', deployer.address);

  const EvidenceAnchor = await ethers.getContractFactory('EvidenceAnchorV3');
  const anchor = await EvidenceAnchor.deploy();
  await anchor.deployed();

  console.log('EvidenceAnchorV3 deployed to:', anchor.address);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
