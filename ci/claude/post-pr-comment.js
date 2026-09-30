const fs = require('fs');

function readTldr(file) {
  if (!file || !fs.existsSync(file))
    return null;
  const tldr = fs.readFileSync(file, 'utf8').trim();
  const lines = tldr.split('\n');
  if (!tldr.startsWith('TL;DR') || lines.length > 4 || tldr.length > 500 ||
      lines.some(line => line.length > 150)) {
    console.log(`Ignoring ${file}: not a valid TL;DR`);
    return null;
  }
  return tldr;
}

module.exports = async ({github, context}) => {
  const jobSummaryUrl = `${process.env.GITHUB_SERVER_URL}/${process.env.GITHUB_REPOSITORY}/actions/runs/${process.env.GITHUB_RUN_ID}`;
  let reviewContent = fs.readFileSync(process.env.REVIEW_FILE, 'utf8');
  const subject = process.env.PATCH_SUBJECT || 'Could not determine patch subject';
  const tldr = readTldr(process.env.TLDR_FILE);
  if (tldr) {
    // KPD emails the comment from its first quoted line on.
    const at = Math.max(reviewContent.search(/^>\s*\S.*$/m), 0);
    reviewContent = reviewContent.slice(0, at) + `> ${subject}\n\n${tldr}\n\n` + reviewContent.slice(at);
  }
  const commentBody = `
\`\`\`
${reviewContent}
\`\`\`

---
AI reviewed your patch. Please fix the bug or email reply why it's not a bug.
See: https://github.com/kernel-patches/vmtest/blob/master/ci/claude/README.md

In-Reply-To-Subject: \`${subject}\`
CI run summary: ${jobSummaryUrl}`;

  await github.rest.issues.createComment({
    issue_number: context.issue.number,
    owner: context.repo.owner,
    repo: context.repo.repo,
    body: commentBody
  });

  await github.rest.issues.addLabels({
    issue_number: context.issue.number,
    owner: context.repo.owner,
    repo: context.repo.repo,
    labels: ["ai-review"],
  });
};
