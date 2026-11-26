# Document-RAG-Chatbot

A team project to build a document-based RAG chatbot with document upload, question answering, and source citations.

한·중 출입국 여행자 휴대품 통관 정책 문서 Q&A 시스템입니다. 공식 정책 PDF를 업로드하고 중국어 또는 한국어로 질문하면, 원문을 검색해 답변을 생성하고 파일명·PDF 물리 페이지·근거 원문을 함께 제시합니다. 근거가 부족하면 답변하지 않으며, 모델 자체 지식으로 수치를 채우지 않습니다.

> 중국어 설명은 [README.md](README.md)를 참고하세요. 본 문서는 2026-11-26 기준 main을 바탕으로 작성했습니다.

## 목차

- [주요 기능](#주요-기능)
- [시스템 구조](#시스템-구조)
- [진행 현황](#진행-현황)
- [저장소 구조](#저장소-구조)
- [로컬 실행](#로컬-실행)
- [API와 응답 상태](#api와-응답-상태)
- [프론트엔드 실행과 백엔드 연동](#프론트엔드-실행과-백엔드-연동)
- [실패 시나리오 검증과 문제 해결](#실패-시나리오-검증과-문제-해결)
- [CI/CD](#cicd)
- [서버 배포](#서버-배포)
- [테스트와 평가](#테스트와-평가)
- [제한 사항과 보안](#제한-사항과-보안)
- [팀 구성](#팀-구성)

## 주요 기능

**문서 관리**

- PDF를 업로드하면 백그라운드에서 파싱·청킹·벡터화가 진행되고, 상태가 「처리 중」에서 「사용 가능」 또는 「실패」로 바뀝니다
- 모든 페이지에 텍스트 레이어가 없는 PDF(스캔본 등)는 실패로 처리하고 사유를 표시합니다. 현재 OCR은 지원하지 않습니다
- 문서 목록에 국가(중국/한국)를 표시하며, 문서를 삭제할 수 있습니다

**질의응답**

- 답변마다 출처를 제시합니다: 파일명, PDF 물리 페이지(1부터 시작, 인쇄된 쪽번호가 아님), 근거 원문. 페이지를 넘는 보완 근거는 연속 페이지 범위로 표시합니다
- 근거가 부족하면 「근거를 찾을 수 없음」을 반환하고 내용을 지어내지 않습니다
- 답변 위에 실제로 사용한 생성 모델 이름을 표시합니다
- 웹 화면은 중국어·한국어 전환을 지원합니다

**두 가지 질의응답 모드** (`RAG_MODE` 설정만 바꾸면 되고 코드는 수정하지 않습니다)

| 모드 | 동작 |
| --- | --- |
| `single`(기본값) | Single RAG: 1회 검색 → 생성 → 인용 검증 |
| `agent` | LangGraph로 구성한 단일 Agent: 국가·출입국 방향 등 필수 조건을 먼저 확인하고, 부족하면 1회 추가 질문합니다. 근거가 부족하면 질의를 재작성해 다시 검색하며, 질문당 검색은 최대 3회입니다. 문서의 국가·방향이 질문에 적용되는지 확인하고, 두 문서의 규정이 충돌하면 양측 근거를 함께 제시합니다. 대화와 추가 질문은 DB에 저장되어 새로고침 후에도 이어서 진행할 수 있고, 실행 중인 질문을 취소할 수 있습니다 |

agent 모드에서 실행 오류가 발생하면 Single RAG로 자동 전환해 답변합니다(제어된 Fallback).

## 시스템 구조

```text
브라우저 (React 웹)
   │  /api
   ▼
백엔드 FastAPI
   ├─ 문서: 업로드 → PyMuPDF 파싱 → 청킹 → BGE-M3 벡터화 → 저장
   └─ 질의응답: single(Single RAG) / agent(LangGraph)
             │                     │
             ▼                     ▼
   PostgreSQL 18 + pgvector     생성 모델 (OpenAI 호환 API)
   (문서, 청크, 벡터, 대화)
```

| 구성 | 사용 기술 |
| --- | --- |
| 프론트엔드 | React 19, TypeScript, Vite |
| 백엔드 | Python 3.12, FastAPI, Uvicorn, SQLAlchemy(async) |
| 데이터베이스 | PostgreSQL 18 + pgvector(1024차원 벡터), Docker로 실행 |
| PDF 파싱 | PyMuPDF, 언어 감지 lingua |
| 임베딩 | `BAAI/bge-m3`, FlagEmbedding으로 로컬 CPU에서 실행 |
| 생성 | OpenAI 호환 Chat Completions API. `GENERATION_PROFILE`로 선택: `furiosa_gemma`(FuriosaAI NPU의 `furiosa-ai/gemma-4-31B-it-FP8-block`) 또는 `legacy`(직접 지정한 모델 주소, 예: 학교 LLM 게이트웨이). 배포된 웹사이트는 현재 `furiosa_gemma`를 사용 |
| Agent | LangGraph |
| 검색 | 벡터 검색 Top-5, 선택적 근거 보완(연령 정의, 페이지를 넘는 열거 조항) |
| CI/CD | GitHub Actions, AWS EC2 배포 |

LangChain, LlamaIndex 등 RAG 프레임워크는 사용하지 않으며, 검색·프롬프트·인용 검증은 `backend/app/qa/`에서 직접 구현했습니다.

## 진행 현황

| 단계 | 기간 | 내용 | 상태 |
| --- | --- | --- | --- |
| MVP | ~10/30 | 업로드, 파싱, Single RAG 질의응답, 출처 표시 | 완료 |
| 1차 개발 | 11/1~11/15 | Agent 구성, 추가 질문, 정식 평가 문항, CI/CD, AWS 배포 | 대부분 병합, 미완료 항목은 2차로 이월 |
| 2차 개발 | 11/16~11/29 | Agent의 백엔드·웹 연동, Single RAG/Agent 전환, NPU Gemma로 생성 전환, 페이지를 넘는 조항, 평가 파이프라인 | 진행 중 |
| 정식 평가·최종 발표 | 12월 | 정식 평가 30문항(LLM-only / Single RAG / Agentic RAG 비교), 12/11 기능 동결 | 예정 |

항목별 상태와 담당자는 GitHub Issues를 참고하세요.

## 저장소 구조

```text
backend/            백엔드 (FastAPI)
  app/api/          HTTP API
  app/documents/    문서 업로드와 처리
  app/ingestion/    PDF 파싱과 청킹
  app/qa/           검색, 근거 보완, 프롬프트, 인용 검증. agent/는 Agent 구성
  app/chat/         질의응답 API. persistent/는 agent 모드의 대화 저장·실행·취소
  app/adapters/     임베딩·생성 모델 호출
  tests/            백엔드 테스트
frontend/           웹 (React)
storage/            DB 스키마 schema.sql과 마이그레이션 migrations/
data/               공개 예시 PDF(3건)와 출처 등록 sources.csv
eval/               스모크 문항, Pilot 문항, 정식 평가 문항과 결과
docs/               인터페이스 설명, Agent 검수 문항
deploy/             서버 배포 스크립트 (「서버 배포」 참고)
scripts/            CI용 검사 스크립트
.github/workflows/  CI/CD
```

정책 문서는 총 5건(한국 2건, 중국 3건)이며 출처는 모두 `data/sources.csv`에 등록되어 있습니다. 저장소에는 공개 예시 PDF 3건만 포함하고, 나머지 2건(KR02, CN03)은 출처만 등록하고 PDF는 올리지 않았습니다. 필요하면 등록된 공식 URL에서 내려받으세요.

## 로컬 실행

아래 명령은 모두 Windows PowerShell에서, 저장소 루트를 기준으로 실행합니다. 터미널 3개가 필요합니다: A 데이터베이스, B 백엔드, C 프론트엔드와 확인 명령.

> `docker compose` 명령은 모두 저장소 루트에서 `--env-file backend/.env`를 붙여 실행합니다. 백엔드는 `backend` 디렉터리에서 실행하며, 디렉터리를 섞어 쓰지 마세요.

검증된 환경: Windows, Python 3.12.10, CPU PyTorch 2.6.0(`torch==2.6.0+cpu`), Docker Desktop 4.97, Node.js 22/24/26. 다른 버전의 호환성은 보장하지 않습니다.

처음 실행할 때 임베딩 모델(약 2 GB)을 내려받으므로 네트워크, 충분한 디스크와 메모리가 필요합니다. CPU 추론 속도는 PC 성능과 문서 크기에 따라 다릅니다. 생성 서비스에는 질문과 검색된 근거가 전송되고 사용량이 차감될 수 있으므로, 권한이 있는 자료만 처리하고 본인 또는 팀이 사용 승인을 받은 Key만 사용하세요.

### 0. Docker Desktop 설치 (최초 1회)

1. Docker 공식 사이트에서 Docker Desktop for Windows를 내려받습니다
2. 설치할 때 **All users**(모든 사용자)를 선택합니다. 기본값인 Per-user 설치는 Windows의 「가상 머신 플랫폼」이 꺼져 있으면 실패합니다
3. 설치 후 PowerShell을 **관리자 권한**으로 열고(시작 메뉴에서 PowerShell 검색 → 우클릭 「관리자 권한으로 실행」) 아래 명령을 실행한 뒤 PC를 재시작합니다:

```powershell
wsl --install --no-distribution
```

4. 재시작 후 Docker Desktop을 열고 왼쪽 아래에 Engine running이 표시될 때까지 기다립니다

### 1. Python 3.12 설치와 가상환경 생성

Python 3.12 최신 버전은 Windows 설치 파일을 제공하지 않으므로, python.org에서 **3.12.10** Windows installer를 내려받으세요. 설치할 때 **Add python.exe to PATH는 체크하지 않습니다**. 기존에 설치된 Python에는 영향이 없습니다.

저장소 루트에서:

```powershell
py -3.12 --version
py -3.12 -m venv .venv
```

이미 `.venv`가 있으면 버전과 용도를 먼저 확인하고, 모르는 환경을 덮어쓰지 마세요. 아래 명령은 가상환경의 인터프리터를 직접 사용하므로 PowerShell 실행 정책을 바꿀 필요가 없습니다.

CPU용 PyTorch를 먼저 설치한 뒤 나머지 의존성을 설치합니다. 패키지가 커서(개당 약 170 MB) 다운로드가 시간 초과되기 쉬우므로 `--timeout 120 --retries 10`을 붙였습니다:

```powershell
.\.venv\Scripts\python.exe -m pip install --timeout 120 --retries 10 -r backend/requirements-cpu.txt
.\.venv\Scripts\python.exe -m pip install --timeout 120 --retries 10 -r backend/requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c "import sys,torch,FlagEmbedding; print(sys.version); print(torch.__version__); print('imports OK')"
```

`imports OK`는 설치가 끝났다는 뜻일 뿐, 모델 추론이 성공했다는 뜻은 아닙니다.

### 2. backend/.env 만들기

저장소 루트에서, 파일이 없을 때만 템플릿을 복사합니다(기존 비밀번호와 Key를 덮어쓰지 않기 위해):

```powershell
if (!(Test-Path backend/.env)) {
    Copy-Item backend/.env.example backend/.env
}
```

메모장으로 `backend/.env`를 열면 항목별 설명이 템플릿 주석에 있습니다. 최소한 아래 항목은 수정해야 합니다:

| 설정 | 입력 내용 |
| --- | --- |
| `POSTGRES_PASSWORD` | 임의의 무작위 문자열(예: 32자) |
| `DATABASE_URL` | 템플릿의 `PASSWORD`를 위와 같은 비밀번호로 변경 |
| `GENERATION_PROFILE` | `furiosa_gemma`(NPU의 Gemma 사용) 또는 `legacy`(직접 지정한 모델 주소 사용) |
| `furiosa_gemma` 사용 시 | FuriosaAI Key를 `backend/.secrets/furiosa.env`에 `FURIOSA_API_KEY=...` 한 줄로 저장하거나, `FURIOSA_API_KEY_FILE`로 다른 경로 지정 |
| `legacy` 사용 시 | `GENERATION_MODEL`, `GENERATION_BASE_URL`, `GENERATION_API_KEY` 입력(예: 학교 LLM 게이트웨이). 주소는 API 루트까지만 쓰고 `/chat/completions`는 붙이지 않습니다 |
| `RAG_MODE` | `single`(기본값) 또는 `agent` |

주의 사항:

- 실제 `.env`와 `.secrets/`는 Git에 올리지 않습니다. Key는 개별 경로로만 전달하고 저장소·스크린샷·단톡방에 올리지 마세요
- 사용자명은 `rag`, DB 이름은 `p4rag`, 로컬 포트는 5432로 고정입니다. 비밀번호에 URL 예약 문자(`@`, `/` 등)가 있으면 인코딩해야 합니다
- DB를 한 번 만든 뒤에는 `POSTGRES_PASSWORD`를 바꿔도 DB 안의 비밀번호가 자동으로 바뀌지 않습니다. 연결이 안 되면 볼륨을 바로 지우지 말고 백엔드 담당자에게 먼저 문의하세요
- `EMBEDDING_BASE_URL`은 비워 두어야 합니다. 현재는 로컬 CPU의 BGE-M3만 지원하며, 값을 넣으면 오류가 납니다
- 1024차원은 모델과 DB가 함께 맞춰진 제약이므로, 차원 값만 바꿔 다른 모델을 쓸 수 없습니다
- `RETRIEVAL_TOP_K` 기본값은 5(1~100 가능)이며, 정식 평가 도중에는 바꾸지 마세요
- `EVIDENCE_COMPLETION_ENABLED=true`는 근거 보완(연령 정의, 페이지를 넘는 열거 조항)을 켭니다. 배포와 시연에서는 켜 둡니다
- 시스템 환경 변수가 `.env`보다 우선합니다. 설정을 바꾼 뒤에는 백엔드를 재시작하세요

### 3. 데이터베이스 시작

터미널 A, 저장소 루트:

```powershell
docker compose --env-file backend/.env up -d postgres
docker compose --env-file backend/.env ps
```

postgres가 `healthy`가 될 때까지 기다립니다. 빈 DB 볼륨으로 처음 시작하면 `storage/schema.sql`이 자동 실행되어 vector 확장과 documents/chunks 테이블이 생성됩니다. 백엔드 시작 시 `init_db()`가 누락된 ORM 테이블과 `document_number_seq`도 생성합니다. 이는 자동 마이그레이션 도구가 아니므로, 기존 DB의 컬럼이 자동으로 업그레이드되지는 않습니다.

테이블과 벡터 컬럼 확인:

```powershell
docker compose --env-file backend/.env exec postgres psql -U rag -d p4rag -c "\dt"
docker compose --env-file backend/.env exec postgres psql -U rag -d p4rag -c "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding';"
```

documents, chunks가 있고 embedding이 `vector(1024)`로 나오면 정상입니다. 파싱 페이지 등 다른 테이블이 더 있을 수 있으므로 「테이블이 2개뿐」을 기준으로 삼지 마세요.

오류에 `bind: An attempt was made to access a socket in a way forbidden by its access permissions`가 나오면 Windows가 5432 포트를 예약한 것입니다. PowerShell을 관리자 권한으로 열어 `net stop winnat`을 실행하고, 터미널 A에서 DB를 다시 시작한 뒤, 시작되면 `net start winnat`을 실행하세요.

### 4. 백엔드 시작

터미널 B, 저장소 루트에서 backend로 이동:

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

DB가 먼저 사용 가능해야 하며, 그렇지 않으면 시작에 실패할 수 있습니다(SQLite로 대체되지 않음). 개발 중 `--reload`를 쓸 수 있지만 모델 연동 단계에서는 끄는 것을 권장하고, 메모리가 적으면 worker를 여러 개 띄우지 마세요. 포트가 사용 중이면 확인 후 다른 포트를 쓰고, 모르는 프로세스를 종료하지 마세요.

### 5. DB 상태 확인

터미널 C:

```powershell
curl.exe -i http://127.0.0.1:8000/health
```

HTTP 200과 아래와 같은 구조가 나오면 정상입니다:

```json
{"status":"ok","database":{"version":"18.x","vector_extension":true}}
```

버전은 실제 반환값을 따르며, vector_extension은 true여야 합니다. 이 API는 BGE-M3나 LLM을 호출하지 않으므로 이것만으로 전체 실행 성공을 판단할 수 없습니다. 대화형 API 문서: http://127.0.0.1:8000/docs

### 6. 최초 모델 로딩과 캐시

보통 첫 청킹에서 tokenizer를, 첫 벡터화에서 모델을 내려받습니다. Uvicorn이 포트를 열었다고 모델이 준비된 것은 아닙니다. 다음 절의 모델 단독 확인을 백엔드 시작 전에 실행하면 미리 내려받고 검증할 수 있습니다(스크립트가 끝난 뒤 서비스를 시작해 두 프로세스가 동시에 모델 메모리를 쓰지 않게 하세요).

첫 처리가 느리면 다운로드·처리 로그를 확인하고 업로드를 반복하지 마세요. 메모리가 적으면 배치 크기를 1로 두고 문서를 하나씩 업로드하세요. 처리 중에는 백엔드를 재시작하지 마세요.

### 7. 모델 단독 확인 (선택, 실제 호출)

backend 디렉터리에서 중국어/한국어 임베딩:

```powershell
..\.venv\Scripts\python.exe -c "from app.adapters.embedding import embed_texts; v=embed_texts(['这是中文测试。','한국어 테스트입니다.']); assert len(v)==2 and all(len(x)==1024 for x in v); print('count=',len(v),'dimensions=',[len(x) for x in v])"
```

`count=2`, `dimensions=[1024, 1024]`가 나오면 정상입니다. 처음에는 모델을 실제로 내려받을 수 있습니다.

생성 서비스(유효한 Key 필요, 사용량 차감 가능):

```powershell
..\.venv\Scripts\python.exe -c "from app.adapters.generation import generate_text; t=generate_text('请用中文简短回复连接成功。'); assert t.strip(); print(t)"
```

비어 있지 않은 텍스트가 나오면 정상이며 문구는 매번 다릅니다. 전체 RAG 검증은 아닙니다.

### 8. PDF 업로드와 질문

/health만으로 판단하지 말고, 최소한 「PDF → READY → 질문 → 답변과 출처」까지 확인하세요. 무관한 파일이 섞이지 않도록 별도의 개발·검수용 DB를 쓰는 것을 권장합니다.

#### 업로드와 상태 확인

터미널 C, 저장소 루트:

```powershell
$base = 'http://127.0.0.1:8000'
$pdf = Join-Path $PWD 'data/中华人民共和国海关进出境行李物品监管办法.pdf'
curl.exe -i -X POST "$base/api/documents" -F "file=@$pdf;type=application/pdf"
```

PowerShell에서는 별칭 차이를 피하기 위해 `curl.exe`를 사용합니다. multipart 필드 이름은 file이어야 합니다. HTTP 201과 문서 레코드가 반환되고 처음 상태는 PROCESSING입니다. 백그라운드에서 파싱·청킹·벡터화·저장이 끝나야 READY가 됩니다.

아래 명령을 반복해 이번 document_id의 상태를 확인합니다:

```powershell
Invoke-RestMethod "$base/api/documents" |
    Select-Object document_id, filename, status, page_count, error_message
```

FAILED이면 error_message와 로그를 확인하고, READY로 직접 수정하지 마세요. 업로드 제한: PDF MIME과 파일 헤더가 올바르고, 비어 있지 않으며, 25 MiB 이하. 원본 PDF는 backend/uploads/{document_id}.pdf에, 파싱 페이지·청크·벡터는 DB에 저장됩니다.

#### 질문

질문에 필요한 정책 문서가 READY인지 먼저 확인하세요. 전체 검수에는 data/의 PDF 3건을 모두 업로드합니다.

```powershell
$payload = @{
    message = '成年旅客从韩国入境中国内地，不属于经常出入境人员，香烟一般可免税携带多少支？'
} | ConvertTo-Json
$reply = Invoke-RestMethod -Method Post -Uri "$base/api/chat" `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload))
$reply | ConvertTo-Json -Depth 8
```

Windows PowerShell 5.1에서는 `Invoke-RestMethod`가 표시하는 중국어·한국어가 깨질 수 있습니다. 표시 문제일 뿐 백엔드 오류가 아니며, PowerShell 7을 쓰거나 `/docs` 화면에서 확인하세요. `RAG_MODE=agent`일 때도 요청 형식은 같습니다. 조건이 부족하면 `NEEDS_CLARIFICATION`, 실행 중이면 202를 반환하며, 결과는 API 절의 execution API로 확인합니다.

ANSWERED이면 answer와 citations가 비어 있지 않아야 하고, 각 인용에 evidence_id, filename, page, quote가 있어야 합니다. 출처 PDF의 해당 물리 페이지에서 quote를 찾아 답변의 조건과 예외를 사람이 직접 확인하세요. 인용 필드가 갖춰졌다고 모든 사실이 맞는 것은 아닙니다. INSUFFICIENT_EVIDENCE는 정상적인 근거 부족 결과이고, SYSTEM_ERROR이면 DB·생성·검증 오류를 확인하세요.

`/docs`에서도 가능합니다: POST /api/documents 업로드 → GET /api/documents로 READY 확인 → POST /api/chat으로 message 전송.

## API와 응답 상태

백엔드를 시작한 뒤 `http://127.0.0.1:8000/docs`에서 대화형 API 문서를 볼 수 있습니다.

**문서**

| API | 요청 / 응답 |
| --- | --- |
| `POST /api/documents` | multipart 필드 `file`. 201과 문서 레코드 반환, 처음 상태 PROCESSING |
| `GET /api/documents` | 문서 레코드 배열 반환 |
| `DELETE /api/documents/{document_id}` | 204 반환, 본문 없음 |

**질의응답**: `RAG_MODE`에 따라 `/api/chat` 아래 API가 다릅니다

| 모드 | API | 설명 |
| --- | --- | --- |
| single | `POST /api/chat` | `message` 전송. 정상 200, 내부 오류 500이며 둘 다 같은 형식의 응답 |
| agent | `POST /api/chat` | `message`와 선택적으로 `session_id` 전송. 추가 질문에 답할 때는 `question_id`와 `conditions`(보완 조건)를 함께 보내며 원래 질문은 바꾸지 않습니다. 실행 중이면 202 반환 |
| agent | `GET /api/chat/sessions` | 대화 목록 |
| agent | `GET /api/chat/sessions/{session_id}` | 한 대화의 전체 질의응답 |
| agent | `DELETE /api/chat/sessions/{session_id}` | 대화 삭제 |
| agent | `GET /api/chat/sessions/{session_id}/questions/{question_id}/execution` | 질문의 실행 상태 조회 |
| agent | `POST /api/chat/sessions/{session_id}/questions/{question_id}/cancel` | 실행 중인 질문 취소 |

**응답 상태**

| 상태 | 언제 | 웹 화면 표시 |
| --- | --- | --- |
| `ANSWERED` | 근거를 찾아 답변 | 답변 + 출처 카드 |
| `INSUFFICIENT_EVIDENCE` | 근거 부족 | 거절 안내, 빈 출처는 표시하지 않음 |
| `NO_READY_DOCUMENTS` | 사용 가능한 문서 없음 | 문서를 먼저 업로드하라는 안내 |
| `SYSTEM_ERROR` | DB, 생성 서비스 등 오류 | 오류 안내와 다시 시도 |
| `NEEDS_CLARIFICATION` | 국가·출입국 방향 등 필수 조건 부족(agent) | 추가 질문과 조건 입력란 |
| `CONFLICTING_EVIDENCE` | 두 문서의 규정이 충돌(agent) | 양측 근거를 함께 표시 |

응답의 `model_name`은 이번에 실제 사용한 생성 모델 이름이며, 웹에서는 답변 위에 표시됩니다. 실제로 답변을 생성하지 않은 경우(사용 가능한 문서 없음, 거절 등)에는 비어 있을 수 있습니다.

출처의 `page`는 1부터 시작하는 PDF 물리 페이지이며 인쇄된 쪽번호가 아닙니다. `filename`은 문서 레코드에서, `quote`는 검색된 원문에서 가져옵니다. 생성 서비스의 Key는 백엔드에만 두며 프론트엔드나 브라우저 요청에 포함되지 않습니다.

## 프론트엔드 실행과 백엔드 연동

아래 명령은 저장소 루트에서 시작합니다. DB와 백엔드 외에 터미널 C에서 프론트엔드를 실행하고, 터미널 3개를 모두 켜 둡니다.

### 1. 프론트엔드 의존성 설치

`frontend/.mise.toml`은 Node.js 22를 지정합니다. 팀에서는 Node.js 24.22.1(npm 11.19.1)과 26.7.0에서도 실행해 보았으나, 모든 버전을 검증한 것은 아닙니다.

```powershell
node --version
npm.cmd --version
cd frontend
npm.cmd ci
```

`npm.cmd ci`는 저장소의 `package-lock.json`으로 설치합니다. PowerShell에서 「npm.ps1을 실행할 수 없습니다」가 나오면 `npm.cmd`를 쓰면 되며, 실행 정책은 바꿀 필요가 없습니다.

### 2. 웹 실행

`frontend` 디렉터리에서:

```powershell
npm.cmd run dev
```

개발 서버와 빌드 미리보기는 기본적으로 `127.0.0.1`에서만 열려 이 PC에서만 접속할 수 있습니다. `FIGMA_DEV_SERVER_HOST` 환경 변수로 바꿀 수 있지만, 로컬 검수에서는 설정하지 않거나 `127.0.0.1`로 두세요:

```powershell
$env:FIGMA_DEV_SERVER_HOST = '127.0.0.1'
npm.cmd run dev
```

개인 생성 서비스 Key를 쓰는 환경에서는 `0.0.0.0`으로 바꾸지 마세요. `/api` 프록시에는 사용자 인증이 없어, 다른 기기가 접속하면 파일을 업로드·삭제하거나 그 Key로 질문할 수 있습니다.

기본 주소는 `http://127.0.0.1:8443`이며 터미널 출력을 따릅니다(HTTP이며 8443이 HTTPS라는 뜻은 아닙니다). 포트가 사용 중이면 오류가 나므로, 이전 프론트엔드 프로세스를 닫거나 다른 포트를 지정하세요:

```powershell
$env:PORT = '8444'
npm.cmd run dev
```

이후 `http://127.0.0.1:8444`로 접속합니다. 종료는 `Ctrl+C`입니다. 화면 수정은 보통 자동 반영되며, 프록시 설정을 바꾸면 프론트엔드를 재시작하세요.

### 3. 프론트엔드 프록시 확인

프론트엔드는 상대 주소 `/api`로 요청하고, `frontend/vite.config.ts`의 개발 서버 프록시가 백엔드 8000번 포트로 전달합니다.

백엔드 `http://127.0.0.1:8000/health`는 정상인데 화면에서 문서 목록을 불러오지 못하면, 프록시 대상의 `localhost`를 `127.0.0.1`로 바꾸세요:

```ts
proxy: {
  '/api': {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
  },
},
```

저장 후 `npm.cmd run dev`를 재시작합니다. Windows에서는 `localhost`가 IPv6로 해석되어 연결에 실패할 수 있습니다.

다른 터미널에서 프록시를 확인할 수 있습니다(웹 포트를 바꿨다면 8443을 바꿔 쓰세요):

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/health'
Invoke-RestMethod 'http://127.0.0.1:8443/api/documents'
```

두 번째 명령은 문서 배열(빈 DB면 빈 배열)을 반환합니다.

### 4. 브라우저 검수

- Documents: `data/`의 PDF를 업로드하면 처리 중으로 표시된 뒤 자동으로 사용 가능으로 바뀝니다. `.docx`를 올리면 PDF만 지원한다는 안내가 나오고, 텍스트 레이어가 없는 이미지 PDF는 실패와 사유가 표시됩니다
- Chat: 질문 후 답변 아래 출처의 파일명·물리 페이지·원문을 확인합니다. 이전 답변의 출처를 누르면 그 답변의 출처가 표시됩니다
- `ANSWERED`, `INSUFFICIENT_EVIDENCE`, `NO_READY_DOCUMENTS`를 확인하고, `SYSTEM_ERROR`는 아래 실패 시나리오로 확인합니다
- `RAG_MODE=agent`일 때: 국가나 출입국 방향이 빠진 질문을 하면 추가 질문이 나오고, 조건을 보완하면 이어서 답변합니다. 새로고침 후에도 대화가 남아 있어야 합니다
- 삭제: 본인 테스트 DB에서 본인 문서만 삭제하세요. 모두 삭제한 뒤 질문하면 사용 가능한 문서가 없다고 안내해야 합니다
- 화면이 열리거나 빌드가 통과한 것만으로 위 항목을 검수한 것은 아닙니다

`frontend` 디렉터리에서 빌드를 확인할 수 있습니다:

```powershell
.\node_modules\.bin\tsc.cmd --noEmit
npm.cmd run build
```

## 실패 시나리오 검증과 문제 해결

### DB 상태 확인 실패

본인 테스트 환경에서, 백엔드가 실행 중이고 처리 중인 작업이 없을 때만 저장소 루트에서 실행하세요:

```powershell
docker compose --env-file backend/.env stop postgres
curl.exe -i http://127.0.0.1:8000/health
docker compose --env-file backend/.env up -d postgres
```

서비스가 실행 중이면 DB 연결 실패 시 HTTP 503과 오류 로그가 나옵니다. 앱 시작 전에 DB가 없으면 시작 자체가 실패할 수 있습니다.

### 업무 실패 시나리오

- 텍스트 없는 PDF/스캔본: 문서 전체를 FAILED로 처리하고 사유를 남깁니다. OCR은 지원하지 않습니다. 실제 이미지만 있는 PDF로 검증하며, 빈 PDF로 대신할 수 없습니다
- 모든 문서 삭제 후 질문: 전용 테스트 DB에서만 하세요. NO_READY_DOCUMENTS가 나오고 생성 호출 로그가 없어야 합니다
- 잘못된 생성 주소: 설정을 백업한 뒤 전용 테스트 환경에서 닫힌 포트로 바꾸고 재시작해 질문하면 500 + SYSTEM_ERROR가 나와야 합니다. 확인 후 설정을 되돌리고 재시작하세요
- 근거 없는 질문: 거절하고 수치를 지어내지 않아야 합니다

| 문제 | 먼저 확인할 것 |
| --- | --- |
| DB 시작 시 5432 포트 access permissions 오류 | Windows의 포트 예약. 「데이터베이스 시작」의 `net stop winnat` 참고 |
| pip 설치 시간 초과 | `--timeout 120 --retries 10`을 붙여 다시 실행 |
| PowerShell에서 중국어·한국어 깨짐 | Windows PowerShell 5.1의 표시 문제. PowerShell 7 또는 `/docs` 사용 |
| PyTorch import 실패 | Python 인터프리터, CPU 의존성과 버전. 전역 환경을 바꿔 가며 시도하지 마세요 |
| 모델 로딩 실패 | 네트워크, 모델 이름, 캐시 권한·무결성, 디스크와 메모리 |
| PROCESSING이 계속됨 | 백엔드 로그, 최초 다운로드, 추론, 프로세스 중단 여부. 자동 복구는 보장하지 않음 |
| 설정이 반영되지 않음 | backend/.env, 같은 이름의 환경 변수, 재시작 여부, 이전 서비스에 접속하고 있는지 |
| HTTP 500 | DB, 생성 서비스 권한·사용량, 시간 초과, 생성 형식, 언어 또는 인용 검증 |
| ANSWERED인데 내용이 부족 | 같은 요청의 Top-5에 조건·예외가 검색되었는지. 상태나 유효한 인용으로 사실 확인을 대신하지 마세요 |

`docker compose ... down -v`는 저장된 데이터를 모두 지우는 작업이므로 일상적인 문제 해결 방법이 아닙니다. 명확한 승인, 버려도 되는 별도 DB, 백업 확인이 있을 때만 사용하세요.

## CI/CD

워크플로는 `.github/workflows/ci-cd.yml`에 있습니다.

**CI**: main으로 보내는 모든 PR과 main이 갱신될 때마다 아래 3가지를 자동 실행하고, 결과를 PR 화면에 표시합니다.

| 검사 | 내용 |
| --- | --- |
| Backend tests | 백엔드 테스트(SQLite와 임시 PostgreSQL), Ruff 코드 검사, mypy 타입 검사, 변경한 코드의 테스트 커버리지 |
| Frontend build | TypeScript 타입 검사, ESLint, 프론트엔드 테스트, 빌드 |
| Deployment script checks | 배포 스크립트와 워크플로 파일 검사 |

PR은 리뷰어의 승인을 받은 뒤 병합합니다. 검사가 실패하면 코드를 고치며, 검사 규칙을 끄거나 테스트를 건너뛰어 통과시키지 않습니다.

**CD**: main이 갱신되고 3가지 검사가 모두 통과하면 「Deploy to EC2」가 승인을 기다립니다. 승인자가 Actions 화면에서 승인해야 AWS EC2에 배포됩니다. DB 마이그레이션(`storage/migrations/`)은 배포 시 자동 실행됩니다. 서버 접속 정보는 저장소 Secrets에 두며 코드에 쓰지 않습니다.

## 서버 배포

배포 절차는 보완 중입니다. 우선 [Issue #47](https://github.com/Kenro66/262RCOSE45700/issues/47)을 참고하세요. DB 마이그레이션 설명은 [deploy/README.md](deploy/README.md)에 있고, 배포 스크립트는 `deploy/`에 있습니다.

배포된 웹사이트에는 로그인 기능이 없어 주소를 아는 사람은 누구나 파일을 업로드·삭제할 수 있고 생성 서비스 사용량도 차감됩니다. 따라서 주소는 팀원, 교수님, 멘토님께만 개별적으로 공유하며 이 저장소나 다른 공개된 곳에 쓰지 않습니다.

## 테스트와 평가

**자동 테스트**: `backend` 디렉터리에서 `..\.venv\Scripts\python.exe -m pytest tests`를 실행합니다. 일부 테스트는 모델 대역이나 SQLite를 사용합니다. `test_issue7.py`, `test_e2e_postgres.py`는 별도의 PostgreSQL 테스트 DB가 필요하고 테이블을 비우므로, 사용 중인 DB를 지정하지 마세요. 프론트엔드는 `frontend` 디렉터리에서 `npm.cmd test`를 실행합니다.

**평가 문항** (`eval/`)

| 문항 세트 | 파일 | 용도 |
| --- | --- | --- |
| 스모크 10문항 | `questions.jsonl`, 결과 `results/results.csv` | MVP 검수, Agent가 Single RAG보다 나빠지지 않았는지 확인 |
| Pilot 40문항 | `pilot43_questions_v1_40.jsonl`, 결과 `results/` | 파라미터 조정과 모델 선택용 |
| 정식 평가 30문항 | `issue44_formal28_review.jsonl`(파일명의 28은 초안 때 이름이며 실제로는 30문항) | 11/20 고정, 이후 파라미터 조정에 사용하지 않음 |
| Agent 검수 문항 | `docs/agent/agent_acceptance_cases_20261110.md` | 추가 질문, 재검색, 충돌 등 Agent 기능 검수 |

정식 평가 문항 구성: 일반 질의 12, 조건 추가 질문 7, 국가·출입국 방향 구분 5, 근거 부족 거절 6. 현재 5건의 문서에서는 실제 정책 충돌을 찾지 못해 충돌 문항은 없으며, 충돌 기능은 모의 데이터로 흐름만 검증했습니다.

평가 스크립트는 `backend/scripts/`에 있습니다. `eval_http_pipeline.py`는 통합 HTTP 평가 파이프라인으로, Single RAG와 Agentic RAG를 모두 `/api/chat`으로 평가하며 202 비동기 폴링과 추가 질문 후 두 번째 요청을 지원하고 결과를 같은 JSONL 형식으로 기록합니다(설명은 `backend/scripts/README_issue48_adapter.md`). 그 밖에 `smoke_issue12.py`, `validate_agent_real.py`, `evaluate_agent_quality.py` 등 실제 호출 스크립트가 있습니다. 이 스크립트들은 외부 모델을 호출해 비용이 들 수 있고 일부는 DB에 쓰므로, 실행 전 설명을 확인하고 별도 테스트 DB를 사용하세요. 자동 판정 결과로 출처 페이지의 수동 확인을 대신할 수 없습니다.

**Pilot 문항에서의 Single RAG 기준선**(11/3, MVP 코드): 답변 가능 문항 완전 정답률 85.7%, 종합 성공률 87.2%, Top-5에 정답 근거 1페이지 이상 포함 100%, 정답 근거 페이지 전체 포함 97.1%, 인용 파일명·페이지·원문 일치 100%, 평균 응답 8.75초.

## 제한 사항과 보안

- 텍스트 레이어가 있는 PDF만 지원하며 OCR은 하지 않습니다
- 검색은 벡터 검색 Top-5만 사용하며 하이브리드 검색은 없습니다. Reranker는 적용하지 않았습니다
- 추가 질문은 1회만 하며, 같은 대화의 이전 질의응답은 답변에 반영하지 않습니다(다중 턴 기억 미지원)
- 충돌은 두 문서가 같은 사안에 대해 실제로 다르게 규정한 경우만 다루며, 신·구 정책이나 적용 대상이 다른 경우는 아직 다루지 않습니다
- 수치 검사는 제한적이어서 품목·단위·조건·법적 의미를 완전히 검증하지 못합니다. 인용이 맞는다고 모든 문장에 근거가 있는 것은 아니므로 결과는 사람이 확인해야 합니다
- 임베딩 모델이나 청킹 방식을 바꾸면 해당 벡터를 다시 만들어야 하며, 이전 벡터와 섞어 쓰지 않습니다
- 실제 `.env`, `backend/.secrets/`, Key, 모델, 캐시, 업로드 파일, DB 백업은 커밋하지 않습니다. 로그를 공유하기 전에 민감 정보를 지우세요
- 웹사이트에는 로그인과 권한 관리가 없으며, 모든 사용자가 하나의 문서 저장소를 공유합니다

## 팀 구성

| 팀원 | 담당 |
| --- | --- |
| 기금전([@Kenro66](https://github.com/Kenro66)) | PM, README, 전체 검수 |
| 왕시기([@sikiww77](https://github.com/sikiww77)) | 백엔드, DB, 질의응답 API, CI/CD와 배포 |
| 손충기([@sun-cs](https://github.com/sun-cs)) | 청킹, 검색, 생성, 인용 검증, Agent |
| 왕일림([@Kruskal086](https://github.com/Kruskal086)) | 웹(Documents, Chat) |
| 강초([@jiangc-666](https://github.com/jiangc-666)) | 평가 문항과 평가 결과 |
