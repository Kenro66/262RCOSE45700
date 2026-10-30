# Document-RAG-Chatbot（한국어）

[中文 README](README.md)

한·중 여행자 휴대품 통관 정책 문서 Q&A 시스템입니다. 정책 PDF를 업로드하고 중국어 또는 한국어로 질문하면, 시스템이 원문을 검색해 답변을 생성하고 **파일명, 물리 페이지 번호, 근거 원문**을 함께 보여 줍니다.

> 이 문서는 2026-10-30 MVP 검수를 위해 작성한 한국어 버전입니다. 팀원용 [중국어 README](README.md)는 검수 과정에서 발견된 설치 문제를 반영해 수정 중이며, 이 한국어 버전도 이후 중국어 README에 맞춰 계속 업데이트할 예정입니다.

## 1. 주요 기능（MVP, 2026-10-30）

| 화면 | 기능 |
| --- | --- |
| Documents | 텍스트형 PDF 업로드, 처리 상태 표시（처리 중 → 준비 완료 / 실패와 실패 사유）, 삭제, 실제 페이지 수 표시 |
| Chat | 자연어 질문, 답변 아래 근거 카드（파일명·페이지·원문）, 정책 최신성 안내 문구 상시 표시 |
| 언어 | 같은 언어로 질문·답변（중국어 질문 → 중국어 답변, 한국어 질문 → 한국어 답변） |

답변 상태는 화면에서 서로 다르게 표시됩니다.

| 상태 | 의미 |
| --- | --- |
| ANSWERED | 근거가 있는 정상 답변, 근거 카드 표시 |
| INSUFFICIENT_EVIDENCE | 업로드된 문서에서 근거를 찾지 못함, 숫자를 지어내지 않고 답변 거절 |
| NO_READY_DOCUMENTS | 사용 가능한 문서가 없음, 먼저 업로드하도록 안내 |
| SYSTEM_ERROR | 모델·서버 오류, 잠시 후 다시 시도하도록 안내 |

## 2. 시스템 구성

```
PDF 업로드 → 텍스트 추출（PyMuPDF） → 조문 단위 분할 → 임베딩（BAAI/bge-m3, 1024차원, 로컬 CPU）
→ PostgreSQL + pgvector 저장 → 질문 시 Top-5 검색 → 생성 모델 답변 → 인용 검증 → 화면 표시
```

- 백엔드：Python 3.12, FastAPI
- 데이터베이스：PostgreSQL 18 + pgvector（Docker）
- 프론트엔드：React + Vite
- 생성 모델：OpenAI 호환 API（현재 학교 Baze 게이트웨이 사용, 최종 모델과 NPU 배포는 확인 중）

## 3. 실행 환경

- Windows, PowerShell 기준으로 확인했습니다.
- Python 3.12（Windows 설치 파일이 있는 마지막 버전은 3.12.10）
- Docker Desktop（WSL 2）
- Node.js 22.12 이상（24, 26.7.0에서도 동작 확인）
- 처음 실행 시 임베딩 모델（약 2GB）을 내려받으므로 네트워크와 디스크 여유 공간이 필요합니다.

## 4. 설치 및 실행

모든 명령은 저장소 루트（`262RCOSE45700`）에서 시작합니다. 창 3개（DB·확인용, 백엔드, 프론트엔드）를 사용합니다.

### 4.1 Python 환경

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-cpu.txt
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt --timeout 120 --retries 10
```

### 4.2 설정 파일

```powershell
if (!(Test-Path backend/.env)) { Copy-Item backend/.env.example backend/.env }
notepad backend/.env
```

메모장에서 다음 항목을 채웁니다. `.env`는 Git에 올리지 않습니다.

| 항목 | 값 |
| --- | --- |
| `POSTGRES_PASSWORD` | 직접 정한 로컬 비밀번호 |
| `DATABASE_URL` | `postgresql://rag:<같은 비밀번호>@localhost:5432/p4rag` |
| `GENERATION_MODEL` | 사용 권한이 있는 모델명 |
| `GENERATION_BASE_URL` | OpenAI 호환 API 루트 주소（`/chat/completions` 제외） |
| `GENERATION_API_KEY` | 본인이 발급받은 키（저장소·캡처·채팅에 올리지 않음） |
| `RETRIEVAL_TOP_K` | 기본값 5 |

### 4.3 데이터베이스

```powershell
docker compose --env-file backend/.env up -d postgres
docker compose --env-file backend/.env ps
```

STATUS가 `healthy`가 되면 완료입니다.

### 4.4 백엔드（새 창）

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

확인：`curl.exe -i http://127.0.0.1:8000/health` → HTTP 200

### 4.5 프론트엔드（새 창）

```powershell
cd frontend
npm.cmd ci
$env:FIGMA_DEV_SERVER_HOST = '127.0.0.1'
npm.cmd run dev
```

브라우저에서 `http://127.0.0.1:8443` 을 엽니다. `FIGMA_DEV_SERVER_HOST`를 지정하지 않으면 개발 서버가 같은 네트워크 전체에 열리므로, 로컬 확인용으로는 지정하는 것을 권장합니다.

### 4.6 Windows 처음 설치 시 참고

| 증상 | 해결 |
| --- | --- |
| Docker Desktop 설치 중 "Virtual Machine Platform" 미활성화로 실패 | 설치 화면에서 **All-users installation** 선택 |
| Docker Desktop 실행 시 "WSL not installed" | 관리자 PowerShell에서 `wsl --install --no-distribution` 실행 후 재부팅 |
| DB 시작 시 `ports are not available ... 5432 ... forbidden` | 관리자 PowerShell에서 `net stop winnat` → DB 시작 → `net start winnat` |
| pip 설치 중 `Read timed out` | 같은 명령을 `--timeout 120 --retries 10`을 붙여 다시 실행 |
| PowerShell에서 한국어·중국어 응답이 깨져 보임 | Windows PowerShell 5.1 표시 문제이며, 웹 화면에서는 정상 표시 |

## 5. 사용 방법

1. **Documents**에서 `data/` 폴더의 정책 PDF를 업로드합니다. 상태가 「준비 완료」가 될 때까지 기다립니다.
2. **Chat**에서 질문합니다. 예：
   - 한국어：`한국에 입국할 때 담배는 일반 물품의 미화 800달러 기본면세범위에 포함되나요?`
   - 중국어：`成年旅客从韩国入境中国内地，不属于经常出入境人员，香烟一般可免税携带多少支？`
3. 답변의 `[1]`, `[2]` 번호를 누르면 오른쪽에 파일명, PDF 페이지, 근거 원문이 표시됩니다. 원본 PDF의 해당 페이지에서 원문을 확인할 수 있습니다.

### 예시 데이터（`data/`）

| 문서 | 기관 | 페이지 |
| --- | --- | --- |
| 여행자 및 승무원 휴대품 통관에 관한 고시 | 대한민국 관세청 | 21 |
| 进境物品关税、增值税、消费税征收办法 | 国务院关税税则委员会 | 7 |
| 中华人民共和国海关进出境行李物品监管办法 | 中华人民共和国海关总署 | 10 |

출처와 이용 조건은 `data/sources.csv`에 기록되어 있습니다.

## 6. MVP 검증 결과（2026-10-30）

- 팀원 1명（README 작성에 참여하지 않음）이 README만 보고 처음부터 설치해 웹 화면에서 PDF 업로드와 질문에 성공했습니다.
- 스모크 테스트 10문항：모두 응답 반환, 상태 일치 9/10, 정답 8/10, 시스템 인용 14건의 페이지를 원본 PDF에서 사람이 확인했습니다.
- 알려진 문제：Q003（예외 조건 누락）, Q008（답할 수 있는 질문을 거절）은 1차 개발에서 검색·근거 판단을 개선합니다.

## 7. 한계와 보안

- 현재는 단일 턴 RAG（Top-k=5）입니다. Agentic RAG, 조건 확인 질문, 충돌 안내는 1차 개발（11/20）에서 구현합니다.
- 텍스트형 PDF만 지원합니다. 스캔 PDF（OCR）, 표·이미지 인식은 지원하지 않으며, 텍스트가 없는 페이지가 절반을 넘으면 「실패」로 처리합니다.
- 시스템은 정책 문서가 최신 유효 버전임을 보장하지 않습니다. 반드시 공식 홈페이지에서 최신 정보를 확인해야 합니다.
- 법률 적용 여부를 자동으로 판단하지 않습니다.
- 로컬 개발용 구성이며, 인증·공개 배포 보안은 아직 갖추지 않았습니다. `.env`와 키는 저장소에 올리지 않습니다.
