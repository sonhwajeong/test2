# 네이버 스마트스토어 크롤러

Playwright를 사용하여 네이버 스마트스토어 상품 상세페이지의 HTML을 크롤링하는 도구입니다.

## 환경 구성

### 필수 요구사항
- Python 3.x
- 가상환경 (venv)

### 설치된 패키지
- `playwright`: 브라우저 자동화
- `openai`: CAPTCHA 자동 해결용 API
- `python-dotenv`: 환경변수 관리

## 초기 설정

### 1. 가상환경 활성화
```bash
source venv/bin/activate  # macOS/Linux
# 또는
venv\Scripts\activate  # Windows
```

### 2. 패키지 설치
```bash
pip install -r requirements.txt
```

### 3. Playwright 브라우저 설치
```bash
playwright install chromium
```

### 4. 환경변수 설정
프로젝트 루트에 `.env` 파일을 생성하고 OpenAI API 키를 설정합니다:
```
OPENAI_API_KEY=your_api_key_here
```

### 5. CSV 파일 준비
- `prod_urls_server3.csv` 파일이 프로젝트 루트에 있어야 합니다
- CSV 파일에는 `n_url` 컬럼이 필요하며, 네이버 스마트스토어 URL(`https://smartstore.naver.com/...`)이 포함되어야 합니다

## 실행 방법

### 기본 실행

로컬에서 실행

```bash
python naver_crawler.py
```

운영에서 실행

```bash
DISPLAY=:99 python naver_crawler.py
```

### 테스트 모드
코드 내 `TEST_LIMIT` 변수를 설정하여 테스트할 URL 개수를 제한할 수 있습니다:
- `TEST_LIMIT = 5`: 5개만 크롤링
- `TEST_LIMIT = None`: 전체 크롤링

## 프로세스 설명

### 1. 초기화 단계
- **로깅 설정**: `logs/YYYY/MM/crawler_YYYYMMDD_HHMMSS.log` 파일에 상세 로그 저장
- **배치 디렉토리 생성**: `output/YYYY/MM/DD/HHMMSS/` 구조로 출력 디렉토리 생성
- **CSV 파일 읽기**: `prod_urls_server3.csv`에서 `n_url` 컬럼의 유효한 URL 추출

### 2. 브라우저 실행
- Chromium 브라우저를 실행 (headless=False로 설정되어 있음)
- 네이버 메인 페이지(`https://www.naver.com`)에 먼저 접속

### 3. URL 크롤링 루프
각 URL에 대해 다음 프로세스를 수행합니다:

#### 3-1. 페이지 접속
- URL로 이동 (`wait_until="domcontentloaded"`)
- 페이지 로딩 대기 (`PAGE_LOAD_WAIT` 초)
- 리다이렉트 확인 및 로깅

#### 3-2. CAPTCHA 감지 및 처리
- HTML에서 CAPTCHA 키워드 검색:
  - "보안 확인을 완료해 주세요"
  - "captcha_wrap"
  - "rcpt_form"
  - "captcha_img_cover"
- CAPTCHA 감지 시:
  1. 캡차 이미지(`#rcpt_img`)와 질문(`#rcpt_info`) 추출
  2. OpenAI Vision API(`gpt-4o-mini`)로 정답 분석
  3. 정답을 입력 필드에 입력 (`#captcha`, `#vcpt_answer` 등)
  4. 확인 버튼(`#cpt_confirm`) 클릭
  5. 해결 여부 확인 (재감지 시 실패 처리)

#### 3-3. HTML 저장
- 성공한 경우: `output/YYYY/MM/DD/HHMMSS/YYYYMMDD_HHMMSS_상품ID.html`
- 실패한 경우: `output/YYYY/MM/DD/HHMMSS/error/YYYYMMDD_HHMMSS_상품ID.html`
- 파일명 형식: `{타임스탬프}_{상품ID}.html`

#### 3-4. 요청 간격 대기
- `REQUEST_INTERVAL` 초 대기 (서버 부하 방지)

### 4. 결과 통계 출력
크롤링 완료 후 다음 통계를 출력합니다:
- 전체 URL 개수
- 성공/실패/CAPTCHA 발생 건수
- 성공률
- 총 소요시간 및 평균 처리시간
- 결과 저장 위치

## 출력 구조

```
output/
└── YYYY/
    └── MM/
        └── DD/
            └── HHMMSS/
                ├── YYYYMMDD_HHMMSS_상품ID.html
                └── error/
                    └── YYYYMMDD_HHMMSS_상품ID.html

logs/
└── YYYY/
    └── MM/
        └── crawler_YYYYMMDD_HHMMSS.log
```

## 설정 가능한 상수

`naver_crawler.py` 파일에서 다음 상수들을 수정할 수 있습니다:

- `CSV_FILE_PATH`: CSV 파일 경로 (기본값: `"prod_urls_server3.csv"`)
- `TEST_LIMIT`: 테스트 모드 URL 개수 제한 (기본값: `5`, 전체 실행 시 `None`)
- `PAGE_LOAD_WAIT`: 페이지 로딩 후 대기 시간 (기본값: `2`초)
- `CAPTCHA_WAIT`: 캡차 제출 후 대기 시간 (기본값: `4`초)
- `REQUEST_INTERVAL`: 요청 간격 대기 시간 (기본값: `1`초)

## 주의사항

- 캡차의 답이 틀리는 경우가 있어 보완이 필요합니다
- openai 4o-mini 한번 호출시 대략 3원정도 사용 (21번 호출당 0.04$ 사용함)

