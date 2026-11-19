/** Keep each HTTP batch within the backend's 20 accounts / 10 models / 60 combinations. */
export function planProbeBatches(accounts: string[], models: string[]) {
  const batches: {accounts: string[]; models: string[]}[] = [];
  for (let m = 0; m < models.length; m += 10) {
    const modelBatch = models.slice(m, m + 10);
    const accountCount = Math.min(20, Math.floor(60 / modelBatch.length));
    for (let a = 0; a < accounts.length; a += accountCount) {
      batches.push({accounts: accounts.slice(a, a + accountCount), models: modelBatch});
    }
  }
  return batches;
}
