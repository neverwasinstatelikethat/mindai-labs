#!/usr/bin/env bash
set -Eeuo pipefail

app_root=/opt/mindai-labs
repo_dir="$app_root/repo"
deployed_file="$app_root/.deployed-commit"

cd "$repo_dir"
git fetch --quiet origin main
target_commit=$(git rev-parse origin/main)
deployed_commit=$(cat "$deployed_file" 2>/dev/null || true)

if [[ "$target_commit" == "$deployed_commit" ]]; then
	exit 0
fi

git checkout --force --detach "$target_commit"
if grep -Eq '^  minio:' compose.yaml; then
	echo "Published compose.yaml still includes unused MinIO; deployment is waiting for its removal." >&2
	exit 1
fi
if ! grep -Fq '127.0.0.1:${BACKEND_PORT:-46617}:8000' compose.yaml || \
	! grep -Fq '127.0.0.1:${FRONTEND_PORT:-43119}:3000' compose.yaml; then
	echo "Published app ports are not bound to loopback; deployment is waiting for the secure Compose config." >&2
	exit 1
fi

docker compose --project-directory "$repo_dir" up -d --build --wait

printf '%s\n' "$target_commit" > "$deployed_file.tmp"
mv "$deployed_file.tmp" "$deployed_file"
