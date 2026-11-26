## 사전 준비

AWS 계정과 EC2 작업 권한을 준비합니다. Ubuntu 인스턴스가 실행 중이며 `ubuntu` 사용자로 터미널에 로그인할 수 있는지 확인합니다. 이 문서는 EC2 터미널에 접속한 뒤 프로젝트를 배포하는 과정부터 다룹니다.

## 1. 당시 배포 환경

| 항목 | 실제 사용 환경 |
| --- | --- |
| AWS 리전 | us-east-1 |
| 운영체제 | Ubuntu 26.04.1 LTS, x86_64 |
| 로그인 | AWS 콘솔의 EC2 Instance Connect, `ubuntu` 사용자 |
| 메모리 | 약 7.6 GiB, 확인 당시 스왑 없음 |
| 루트 디스크 | 30 GiB |
| 프로젝트 디렉터리 | `/home/ubuntu/262RCOSE45700` |
| Python | 시스템 3.14.4, 프로젝트에는 uv로 3.12.10을 별도 설치 |
| 백엔드 | FastAPI / Uvicorn, `127.0.0.1:8000`에서 수신 |
| 데이터베이스 | Docker Compose에서 PostgreSQL 18 + pgvector 실행 |
| 임베딩 모델 | EC2 CPU에서 BAAI/bge-m3 실행 |
| 생성 모델 | 백엔드가 팀에 사용 권한이 있는 생성 API 호출 |
| 프런트엔드 | 당시 EC2에서 Node.js / npm으로 빌드 |
| 웹 접속 | Nginx, HTTP 80번 포트 |
| 정적 파일 디렉터리 | `/var/www/rag-chatbot` |

## 2. EC2 접속 및 네트워크 설정

AWS 콘솔에서 Ubuntu 인스턴스를 선택하고 **연결 → EC2 Instance Connect**를 통해 `ubuntu` 사용자로 로그인합니다. 사이트는 인스턴스에 연결된 탄력적 IP로 접속합니다.

| 포트 | 용도 |
| --- | --- |
| 22 | SSH 관리 접속. 관리 및 배포에 필요한 출발지에서 허용 |
| 80 | HTTP 웹 접속. 당시에는 공인 IPv4 접속 허용 |
| 8000 | 백엔드. 인스턴스 내부에서만 수신 |
| 5432 | 데이터베이스. 인스턴스 내부에만 포트 매핑 |
| 443 | 당시 HTTPS 미설정 |

EC2 터미널에서는 `127.0.0.1`로 서비스를 확인하고, 개인 컴퓨터의 브라우저에서는 `http://<EC2_HOST>/`에 접속합니다.

## 3. 프로젝트 코드 내려받기

~~~bash
sudo apt update
sudo apt install -y git
cd /home/ubuntu
git clone https://github.com/Kenro66/262RCOSE45700.git
cd /home/ubuntu/262RCOSE45700
ls
~~~

디렉터리에 `backend`, `frontend`, `storage`, `docker-compose.yml`이 있는지 확인합니다. 저장소를 이미 복제했다면 기존 디렉터리를 사용합니다.

## 4. 백엔드 환경 파일 설정

템플릿에서 서버용 설정 파일을 만들고, 기존 `.env` 파일은 그대로 사용합니다.

~~~bash
cd /home/ubuntu/262RCOSE45700/backend
if [ ! -f .env ]; then cp .env.example .env; fi
nano .env
~~~

팀에서 사용을 승인한 설정값을 입력합니다. 비밀값은 서버에만 저장합니다.

| 설정 | 당시 값 또는 용도 |
| --- | --- |
| `POSTGRES_PASSWORD` | PostgreSQL 비밀번호 |
| `DATABASE_URL` | 로컬 5432번 포트의 `p4rag` 데이터베이스에 `rag` 사용자로 접속. 데이터베이스와 동일한 비밀번호 사용 |
| `EMBEDDING_MODEL` | BAAI/bge-m3 |
| `EMBEDDING_BASE_URL` | CPU 모드에서는 빈 값 |
| `EMBEDDING_DIMENSION` | 1024, 데이터베이스 벡터 열의 차원과 일치 |
| `EMBEDDING_BATCH_SIZE` | 1 |
| `EMBEDDING_MAX_LENGTH` | 8192 |
| `EMBEDDING_CPU_THREADS` | 1 |
| `GENERATION_MODEL` | 팀에서 사용을 승인한 생성 모델 이름 |
| `GENERATION_BASE_URL` | 생성 API 기본 URL. `/chat/completions`는 중복해서 붙이지 않음 |
| `GENERATION_API_KEY` | 팀에서 사용을 승인한 생성 API 키. 백엔드에만 저장 |
| `GENERATION_TIMEOUT_SECONDS` | 120 |
| `RETRIEVAL_TOP_K` | 5 |
| `EVIDENCE_COMPLETION_ENABLED` | true |

나머지 설정은 `.env.example`을 참고합니다. 연결 문자열에 넣는 비밀번호의 특수 문자는 URL 인코딩합니다. 기존 데이터베이스의 비밀번호는 데이터베이스에서 직접 변경합니다.

## 5. Python 3.12 가상 환경 만들기

uv로 프로젝트의 Python 3.12.10 가상 환경을 만듭니다.

~~~bash
sudo apt install -y curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
sh /tmp/uv-install.sh
"$HOME/.local/bin/uv" python install 3.12.10
cd /home/ubuntu/262RCOSE45700
"$HOME/.local/bin/uv" venv --python 3.12.10 --seed .venv
source .venv/bin/activate
python --version
~~~

출력이 Python 3.12.10인지 확인합니다. `.venv`가 이미 있으면 `.venv/bin/python`을 바로 사용할 수 있습니다.

## 6. Docker 설치 및 데이터베이스 실행

~~~bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo docker --version
sudo docker compose version
~~~

당시 배포에는 Docker 29.1.3과 Compose 2.40.3을 사용했습니다. 다음 명령으로 데이터베이스를 실행합니다.

~~~bash
cd /home/ubuntu/262RCOSE45700
sudo docker compose --env-file backend/.env up -d postgres
sudo docker compose --env-file backend/.env ps
~~~

상태가 `healthy`인지 확인합니다. PostgreSQL은 `127.0.0.1:5432`에서 `p4rag` 데이터베이스를 제공하며 사용자는 `rag`입니다. 빈 볼륨으로 처음 실행하면 `storage/schema.sql`이 실행되고, 이후에는 기존 볼륨을 재사용합니다. 문제를 해결할 때도 데이터 볼륨을 유지합니다. `down -v`를 실행하면 볼륨이 삭제됩니다.

## 7. 백엔드 의존성 설치 및 수동 실행

CPU용 PyTorch를 먼저 설치한 뒤 나머지 백엔드 의존성을 설치합니다.

~~~bash
cd /home/ubuntu/262RCOSE45700
.venv/bin/python -m pip install -r backend/requirements-cpu.txt
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python -m pip check
~~~

당시 `torch 2.6.0+cpu`를 사용했고 `pip check`에서 의존성 충돌이 없었습니다.

최초 배포에서는 Uvicorn을 포그라운드로 실행하고 로그를 저장했습니다.

~~~bash
cd /home/ubuntu/262RCOSE45700/backend
../.venv/bin/python -m uvicorn app.main:app --env-file .env \
  --host 127.0.0.1 --port 8000 2>&1 | tee -a "$HOME/rag-backend.log"
~~~

백엔드가 실행 중인 터미널을 열어 둔 채 다른 터미널에서 상태를 확인합니다. `Ctrl+C`를 누르면 백엔드가 종료됩니다.

~~~bash
curl -i http://127.0.0.1:8000/health
~~~

당시 HTTP 200이 반환되었고 `status=ok`, PostgreSQL 18.6, `vector_extension=true`를 확인했습니다. 질의응답은 별도로 검증합니다.

## 8. EC2에서 프런트엔드 빌드

~~~bash
sudo apt update
sudo apt install -y nodejs npm
node --version
npm --version
cd /home/ubuntu/262RCOSE45700/frontend
npm ci
npm run build
~~~

당시 Node.js v22.22.1과 npm 9.2.0을 사용했습니다. 웹사이트는 `frontend/dist`의 빌드 결과물을 사용합니다.

## 9. Nginx로 웹 페이지 제공 및 API 프록시 설정

Nginx를 설치하고 프런트엔드 빌드 결과물을 사이트 디렉터리에 복사합니다.

~~~bash
sudo apt install -y nginx
sudo mkdir -p /var/www/rag-chatbot
sudo cp -r /home/ubuntu/262RCOSE45700/frontend/dist/. /var/www/rag-chatbot/
sudo nano /etc/nginx/sites-available/rag-chatbot
~~~

사이트 설정은 다음과 같습니다.

~~~nginx
server {
    listen 80;
    server_name _;

    root /var/www/rag-chatbot;
    index index.html;
    client_max_body_size 25m;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
~~~

`proxy_pass` 주소 끝의 슬래시를 생략해 `/api/` 접두사를 유지합니다. `25m` 제한은 업로드에 추가되는 형식 데이터를 포함한 전체 요청 본문에 적용됩니다.

최초 활성화 시 사이트 링크를 만들고 기본 사이트 링크를 제거합니다.

~~~bash
sudo ln -s /etc/nginx/sites-available/rag-chatbot /etc/nginx/sites-enabled/rag-chatbot
sudo unlink /etc/nginx/sites-enabled/default
sudo nginx -t
# 설정 검사에 성공했을 때만 다음 명령을 실행합니다.
sudo systemctl reload nginx
~~~

사이트 링크 변경은 처음 활성화할 때만 진행합니다. 당시 중복된 `server` 설정으로 충돌이 발생했고, 중복 설정을 정리한 뒤 `nginx -t`를 통과했습니다.

## 10. 웹사이트 및 실제 기능 검증

당시 EC2에서 웹 페이지와 문서 API를 다음과 같이 확인했습니다.

~~~bash
curl -I http://127.0.0.1/
curl -i http://127.0.0.1/api/documents
~~~

두 요청에 모두 HTTP 200이 반환되었고, 빈 데이터베이스의 문서 목록은 `[]`였습니다. 개인 컴퓨터에서 `http://<EC2_HOST>/`에 접속해 PDF를 업로드한 뒤 상태가 `READY`가 되면 질문하고 답변의 출처를 확인합니다.

- 터미널 기록에서 실제 PDF 여러 건이 `READY` 상태에 도달했고 검색 질의의 임베딩 추론이 성공했습니다. 배포 담당자는 2026-11-15에 웹 질의응답이 정상 동작하고 답변의 출처를 확인했다고 기록했습니다.
- 생성 API 주소, 인증, 모델 이름에 오류가 있으면 백엔드의 `.env`와 로그를 확인합니다. PDF 처리에 실패하면 파일 상태와 원인을 확인합니다. 스캔 PDF에는 텍스트 레이어가 없을 수 있습니다.

## 11. 수동 배포 단계에서의 서비스 시작

인스턴스를 재시작하면 Docker, Nginx, 데이터베이스를 먼저 실행한 뒤 7절의 명령으로 백엔드를 포그라운드에서 시작합니다.

~~~bash
cd /home/ubuntu/262RCOSE45700
sudo systemctl start docker nginx
sudo docker compose --env-file backend/.env up -d --wait postgres
~~~

이후에는 `nohup`으로 Uvicorn을 백그라운드에서 실행했습니다. 로그는 프로젝트 루트의 `backend.log`에 저장됩니다. 시작하기 전에 8000번 포트를 사용하던 프로세스가 종료됐는지 확인합니다.

~~~bash
cd /home/ubuntu/262RCOSE45700/backend
nohup ../.venv/bin/python -m uvicorn app.main:app --env-file .env \
  --host 127.0.0.1 --port 8000 > ../backend.log 2>&1 < /dev/null &
~~~

인스턴스를 재시작한 뒤에는 `nohup` 프로세스를 다시 수동으로 실행합니다. CI/CD를 도입할 때는 기존 `nohup` 프로세스를 중지하고 `rag-backend.service`가 백엔드를 관리하도록 전환했습니다.

프로젝트 운영 방침에 따라 사용을 중단할 때는 AWS 콘솔에서 **Stop**을 선택합니다.

## 12. GitHub Actions로 후속 배포

### 12.1 배포 기록

CI에서 프런트엔드를 빌드하고, Actions에서 SSH로 EC2를 업데이트하며, systemd의 `rag-backend.service`가 백엔드를 관리합니다.

- 처음 연동했을 때 기존 데이터베이스에 문서 메타데이터 필드가 없었습니다. 데이터베이스를 수동으로 백업하고 `storage/migrations/045_document_metadata.sql`을 실행한 뒤, 같은 워크플로의 세 번째 실행에서 커밋 `2274d737` 배포에 성공했습니다. [첫 배포 성공 기록](https://github.com/Kenro66/262RCOSE45700/actions/runs/44098492059)
- 이어서 커밋 `e043a8b3` 배포에 성공했고 백엔드, 데이터베이스, 웹 페이지, API 프록시 점검을 통과했습니다. [배포 기록](https://github.com/Kenro66/262RCOSE45700/actions/runs/44112390236)
- 2026-11-18에 커밋 `34bea0d` 배포에 성공했습니다. 로그에서 백엔드, 데이터베이스, 웹 페이지, `/api` 프록시 점검 통과를 확인했습니다. [배포 로그](https://github.com/Kenro66/262RCOSE45700/actions/runs/44287172596/job/134744060118)

### 12.2 배포 설정

EC2를 실행 상태로 유지하고 SSH 접속을 허용합니다. Actions 전용 공개 키를 등록하고 EC2가 GitHub 저장소에 비대화형으로 접근할 수 있는지 확인합니다. `deploy/rag-backend.service`를 설치한 뒤 서비스 실행 사용자, Python 경로, 프로젝트 디렉터리를 확인합니다. 기존 데이터베이스 볼륨을 재사용하고, 배포 전에 필요한 마이그레이션을 확인하고 백업합니다.

| GitHub 설정 | 이름 | 값 또는 출처 |
| --- | --- | --- |
| Secrets | `EC2_HOST` | EC2 콘솔에 표시되는 현재 공인 IP 또는 DNS |
| Secrets | `EC2_USER` | ubuntu |
| Secrets | `EC2_SSH_KEY` | 소유자가 GitHub에 직접 입력하는 전용 개인 키 전체 |
| Secrets | `EC2_KNOWN_HOSTS` | 신뢰할 수 있는 콘솔에서 확인한 서버 호스트 키 정보 |
| Variables | `EC2_REPO_DIR` | `/home/ubuntu/262RCOSE45700` |
| Variables | `EC2_FRONTEND_ROOT` | `/var/www/rag-chatbot` |
| Variables | `ENABLE_EC2_DEPLOY` | 처음에는 `false`, 배포를 활성화할 때 `true` |

신뢰할 수 있는 EC2 터미널에서 `/etc/ssh/ssh_host_ed25519_key.pub`을 읽고 `호스트 주소 ssh-ed25519 공개 키의 Base64 값` 형식으로 `EC2_KNOWN_HOSTS`를 입력합니다. 호스트 주소는 `EC2_HOST`와 같아야 합니다.

관리자는 `production` 환경에서 **Required reviewers**를 활성화하고 `main` 브랜치의 배포만 허용합니다. 팀 방침에 따라 셀프 승인과 보호 규칙 우회를 제한합니다. 검토자를 두 명 지정하면 한 명의 승인으로 배포할 수 있습니다. [GitHub 환경 규칙](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)

### 12.3 일반 배포 절차

1. 팀원이 PR을 올리면 **Backend tests**, **Frontend build**, **Deployment script checks**가 자동 실행됩니다. 배포는 `main`에 병합된 뒤 진행됩니다.
2. 팀의 코드 리뷰 절차에 따라 `main`에 병합하면 세 가지 CI 검사가 다시 실행됩니다.
3. 세 검사를 모두 통과하고 `ENABLE_EC2_DEPLOY`가 `true`이면 `production` 환경의 승인을 기다립니다.
4. 검토자는 해당 Actions 실행에서 **Review deployments → production → Approve and deploy**를 선택합니다.
5. Actions는 SSH로 접속해 서버를 검증된 커밋으로 fast-forward 업데이트합니다. 기존 데이터베이스를 실행하고 백엔드 의존성과 서비스를 갱신한 뒤 CI에서 빌드한 프런트엔드를 배포합니다.
6. 상태 확인 API, 웹 페이지, 문서 API를 자동으로 점검합니다. 유지보수 담당자는 실제 파일 업로드, 질의응답, 답변 출처를 확인합니다.

처음 활성화하거나 수동 실행할 때는 **Actions → CI/CD → Run workflow → main**을 선택합니다. 수동 실행도 CI 검사와 승인을 거칩니다. [GitHub 배포 승인 안내](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments)

배포를 활성화한 뒤에는 워크플로를 별도로 실행해야 합니다. 배포를 일시 중지했다면 Actions에서 이미 등록된 작업을 확인하거나 취소합니다.

### 12.4 배포 실패 및 복구

- EC2가 중지되어 있어도 CI는 실행됩니다. 배포는 SSH 연결 실패로 중단됩니다. 인스턴스를 시작한 뒤 최신 `main`에서 워크플로를 다시 실행하고 승인합니다. 기존 작업이 승인 대기 중이면 인스턴스 주소를 확인한 뒤 진행할 수 있습니다.
- `main`에 새 커밋이 추가되면 이전 배포 작업은 `main advanced` 메시지와 함께 중단됩니다. 2026-11-18에는 배포 대상 `66ae964`가 `42b94ab`보다 뒤처져 워크플로가 중단됐습니다. 이 경우 최신 `main`에 해당하는 실행을 사용합니다. [중단 로그](https://github.com/Kenro66/262RCOSE45700/actions/runs/44294893630/job/134770610084)
- 당시 `deploy.sh`는 데이터베이스 볼륨을 재사용했고 마이그레이션은 유지보수 담당자가 수행했습니다. 현재 CD에서는 등록되지 않은 마이그레이션을 자동 실행합니다. 배포 중 백엔드가 잠시 중단될 수 있습니다. 장애가 발생하면 로그, 커밋 ID, 백업을 보존하고 유지보수 담당자가 복구합니다. [EC2 데이터베이스 마이그레이션 절차](README.md)
- 서버에서 로컬 `.env`, 데이터, 모델 설정, 캐시를 관리합니다. 비밀값은 서버에서 별도로 갱신합니다.

배포 후 Actions의 **Deploy to EC2** 로그에서 실제 배포된 커밋과 점검 결과를 확인합니다.
