#!/usr/bin/env bash
# 更新现有 Ubuntu 环境中的部署，复用原数据库卷。
set -euo pipefail

fail() { printf 'Deployment stopped: %s\n' "$*" >&2; exit 1; }
[[ $# -eq 4 ]] || fail "usage: deploy.sh COMMIT FRONTEND_ARCHIVE REPO_DIR FRONTEND_ROOT"
target_sha=$1
archive=$2
repo_arg=$3
frontend_arg=$4
[[ "$target_sha" =~ ^[0-9a-f]{40}$ ]] || fail "expected a full commit SHA"
[[ "$repo_arg" == /* && "$frontend_arg" == /* ]] || fail "paths must be absolute"
[[ -f "$archive" ]] || fail "frontend archive missing"

repo_dir=$(realpath -e -- "$repo_arg")
frontend_root=$(realpath -e -- "$frontend_arg")
[[ "$repo_dir" != / && -d "$repo_dir/.git" ]] || fail "repository directory not found"
# 仅支持现有网站目录或本仓库的 Vite 构建输出目录。
case "$frontend_root" in
  /var/www/?*|"$repo_dir/frontend/dist") ;;
  *) fail "frontend root must be below /var/www or the repository's frontend/dist" ;;
esac
[[ -d "$frontend_root" ]] || fail "frontend root must be a directory"
cd "$repo_dir"

# 通过锁确保本机上的工作流部署与手动部署不会同时执行。
exec 9> .git/ec2-deploy.lock
flock -n 9 || fail "another deployment is running"
[[ "$(git branch --show-current)" == main ]] || fail "EC2 checkout must be on main"
if ! git diff --quiet || ! git diff --cached --quiet; then
  fail "tracked local edits; reconcile them first"
fi
[[ -f backend/.env && -x .venv/bin/python ]] || fail "existing .env / virtualenv missing"
sudo -n true || fail "deployment user needs configured noninteractive sudo"
[[ "$(sudo -n systemctl show rag-backend.service --property=WorkingDirectory --value)" == "$repo_dir/backend" ]] \
  || fail "install the matching rag-backend.service first"
for tool in git tar curl rsync flock; do
  command -v "$tool" >/dev/null || fail "$tool not installed"
done
sudo -n nginx -t

# 拒绝过时的排队任务。绝不执行 git reset --hard、git clean 或强制更新分支。
git fetch --no-tags origin main
[[ "$(git rev-parse FETCH_HEAD)" == "$target_sha" ]] || fail "main advanced; deploy its newer CI run"
git merge-base --is-ancestor HEAD "$target_sha" || fail "EC2 main is not an ancestor of target"
previous_sha=$(git rev-parse HEAD)
printf 'Updating %s -> %s\n' "$previous_sha" "$target_sha"

staging=$(mktemp -d /tmp/rag-frontend.XXXXXXXX)
cleanup() {
  # 此目录由 mktemp 创建，其中仅存放临时解压的构建文件。
  [[ "$staging" == /tmp/rag-frontend.* && -d "$staging" ]] && rm -rf -- "$staging"
}
trap cleanup EXIT

# Python 的 data 过滤器会拒绝指向临时解压目录之外的路径。
.venv/bin/python - "$archive" "$staging" <<'PY'
import pathlib
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as archive:
    archive.extractall(sys.argv[2], filter="data")
if not (pathlib.Path(sys.argv[2]) / "index.html").is_file():
    raise SystemExit("Frontend archive has no index.html")
PY

# 以上操作均不会修改正在运行的应用。
git merge --ff-only "$target_sha"
# 保持当前仓库名称，并将原 Compose 文件列在首位，以复用 postgres_data 数据库卷。
compose=(sudo -n docker compose --env-file backend/.env
  -f docker-compose.yml -f deploy/compose.production.yml)
"${compose[@]}" up -d --wait postgres
# 部署与后端共用现有虚拟环境：更新依赖前先停止后端。
# 失败时会明确报错；不保证零停机，也不提供依赖回滚。
sudo -n systemctl stop rag-backend.service
recover_service() {
  result=$?
  if (( result != 0 )); then
    printf 'Deployment failed at commit %s (previous %s).\n' "$target_sha" "$previous_sha" >&2
    printf 'Check journalctl -u rag-backend and restore the service before retrying deployment.\n' >&2
    sudo -n systemctl start rag-backend.service || true
  fi
  cleanup
  exit "$result"
}
trap recover_service EXIT
.venv/bin/python -m pip install -r backend/requirements-cpu.txt
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python -m pip check
# 停止旧后端后再清理旧的聊天快照，避免旧进程在迁移后写回证据和答案。
if ! bash deploy/migrate.sh |
  "${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U rag -d p4rag; then
  fail "database migration failed"
fi
sudo -n systemctl start rag-backend.service

ready=false
for (( attempt=0; attempt<30; attempt++ )); do
  if curl --fail --silent --max-time 8 http://127.0.0.1:8000/health |
    .venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status") == "ok" and d.get("database", {}).get("vector_extension") is True else 1)' 2>/dev/null; then
    ready=true
    break
  fi
  sleep 2
done
[[ "$ready" == true ]] || fail "backend/database health check failed"

# 先复制文件名包含哈希值的静态资源，最后替换 index.html；保留旧资源以供已打开的浏览器页面使用。
# 不使用 --delete：部署时不删除现有网页文件或静态资源。
sudo -n rsync -r --chmod=D755,F644 --exclude=index.html "$staging/" "$frontend_root/"
sudo -n install -m 644 "$staging/index.html" "$frontend_root/index.html"
sudo -n systemctl start nginx
curl --fail --silent --max-time 15 http://127.0.0.1/ >/dev/null
curl --fail --silent --max-time 15 http://127.0.0.1/api/documents |
  .venv/bin/python -c 'import json,sys; sys.exit(0 if isinstance(json.load(sys.stdin), list) else 1)'
printf 'Deployed commit %s; backend, database, webpage and /api proxy checked.\n' "$target_sha"
printf 'Real PDF upload / model answer still require application acceptance testing.\n'
