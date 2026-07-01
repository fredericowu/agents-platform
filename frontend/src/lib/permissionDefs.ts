export const PERMISSION_DEFS: { key: string; label: string; description: string; defaultOn?: boolean }[] = [
  { key: "docker", label: "Docker", description: "Mount the Docker socket — enables docker ps, docker run, etc." },
  { key: "github", label: "GitHub / Git", description: "Mount ~/.gitconfig and ~/.config/gh (gh CLI auth) — enables gh repo list/pr/issue and HTTPS git operations." },
  {
    key: "share_network", label: "Share network", defaultOn: true,
    description: "Join aw-sandbox's network namespace instead of an isolated bridge — lets the agent reach 127.0.0.1 ports on the host (awserv, redis, postgres, agents-platform itself). On by default for now.",
  },
];
