""" 
네이버 스마트스토어 크롤러 모듈
================================
네이버 스마트스토어 크롤링 전용 모듈입니다.
CAPTCHA 감지, 해결, HTML 렌더링 검증, 파일 저장 등의 기능을 제공합니다.

주요 기능:
- CAPTCHA 자동 감지 및 OpenAI Vision API를 통한 해결
- HTML 렌더링 상태 검증
- 파일 저장 (성공/에러/삭제된상품/렌더링실패별 분류)
"""

import time
import os
import re
import traceback
import socket
import subprocess
import pickle
from datetime import datetime
from urllib.parse import urlparse
from openai import OpenAI
from playwright.sync_api import sync_playwright
import pymysql


# ============================================================================
# 설정 및 상수
# ============================================================================
# 대기 시간 설정 (초)
PAGE_LOAD_WAIT = 3      # 페이지 로딩 후 대기
CAPTCHA_WAIT = 4        # 캡차 제출 후 대기
REQUEST_INTERVAL = 1    # 상세 페이지 크롤링 후 간격 대기

# 캡차 재시도 설정
CAPTCHA_MAX_RETRIES = 3
CAPTCHA_RETRY_WAIT = 8

# 메모리 최적화 설정
PAGE_REFRESH_INTERVAL = 50  # 50건마다 페이지 재생성

# 네이버 메인 페이지
NAVER_HOME_URL = 'https://www.naver.com'

# ============================================================================
# DB 설정
# ============================================================================
DB_CONFIG = {
    'host': '172.31.11.208',
    'user': 'crawler_app_user',
    'password': '!Crawler@1234%',
    'database': 'crawl',
    'charset': 'utf8mb4',
    'connect_timeout': 10,
    'read_timeout': 30,
    'write_timeout': 30
}


# ============================================================================
# CAPTCHA 감지 및 해결
# ============================================================================
def detect_captcha(html_content, logger):
    """
    HTML에서 CAPTCHA 페이지 여부를 확인합니다.

    Args:
        html_content: 페이지 HTML 내용
        logger: 로거 객체

    Returns:
        bool: CAPTCHA 페이지면 True, 아니면 False
    """
    captcha_keywords = [
        "보안 확인을 완료해 주세요",
        "captcha_wrap",
        "rcpt_form",
        "captcha_img_cover"
    ]

    for keyword in captcha_keywords:
        if keyword in html_content:
            logger.debug(f"CAPTCHA 키워드 감지: '{keyword}'")
            return True

    return False


def solve_captcha_with_openai(page, openai_api_key, logger):
    """
    OpenAI Vision API로 캡차 이미지를 분석하여 정답을 반환합니다.

    Args:
        page: Playwright Page 객체
        openai_api_key: OpenAI API 키
        logger: 로거 객체

    Returns:
        str: 캡차 정답 문자열 (실패 시 None)
    """
    try:
        logger.info("OpenAI API로 캡차 해결 시도 중...")

        img_locator = page.locator("#rcpt_img")
        desc_locator = page.locator("#rcpt_info")

        try:
            img_locator.wait_for(state="visible", timeout=5000)
        except:
            logger.warning("[캡차 오류] 캡차 이미지를 찾을 수 없습니다.")
            return None

        image_src = img_locator.get_attribute("src")
        question_text = desc_locator.inner_text()

        if not image_src:
            logger.warning("[캡차 오류] 이미지 소스를 가져올 수 없습니다.")
            return None

        logger.info(f"📝 캡차 질문 텍스트: {question_text}")

        logger.info("🤖 OpenAI API로 캡차 이미지 분석 중...")
        client = OpenAI(api_key=openai_api_key)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"이것은 캡차 문제입니다. 다음 질문에 대한 정답 단어만 딱 하나 출력하세요. "
                               f"(예: 질문이 '가게 위치는 명덕산길 [?] 입니다'라면 '[?]'에 들어갈 단어만 출력). "
                               f"질문: {question_text}"
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": image_src}
                    }
                ],
            }],
            max_tokens=50,
        )

        answer = response.choices[0].message.content.strip()
        logger.info(f"✅ OpenAI 응답 (추출된 정답): {answer}")
        return answer

    except Exception as e:
        logger.error(f"[캡차 해결 오류] {e}")
        logger.debug(traceback.format_exc())
        return None


def submit_captcha_answer(page, answer, logger):
    """
    캡차 정답을 입력하고 제출합니다.

    Args:
        page: Playwright Page 객체
        answer: 입력할 정답
        logger: 로거 객체

    Returns:
        bool: 제출 성공 여부
    """
    input_selectors = ['#captcha', '#vcpt_answer', 'input[name="captcha"]']

    try:
        filled = False
        for selector in input_selectors:
            if page.locator(selector).count() > 0 and page.is_visible(selector):
                page.fill(selector, answer)
                logger.info(f"정답 입력 완료 (선택자: {selector})")
                filled = True
                break

        if not filled:
            for selector in input_selectors:
                if page.locator(selector).count() > 0:
                    page.fill(selector, answer, force=True)
                    logger.warning(f"정답 강제 입력 (선택자: {selector})")
                    filled = True
                    break

        if not filled:
            logger.error("[캡차 제출 오류] 입력 필드를 찾을 수 없습니다.")
            return False

        page.click('#cpt_confirm')
        logger.info("확인 버튼 클릭, 결과 대기 중...")
        time.sleep(CAPTCHA_WAIT)

        return True

    except Exception as e:
        logger.error(f"[캡차 제출 오류] {e}")
        logger.debug(traceback.format_exc())
        return False


# ============================================================================
# HTML 상태 확인
# ============================================================================
def check_html_status(html_content, logger):
    """
    HTML이 정상적으로 렌더링되었는지, 상품이 존재하는지 확인합니다.

    Args:
        html_content: 페이지 HTML 내용
        logger: 로거 객체

    Returns:
        str: 'rendered' (정상), 'no_render' (렌더링 실패), 'no_data' (상품 없음)
    """

    # 1. 상품 존재 여부 확인
    no_product_patterns = [
        '상품이 존재하지 않습니다',
        '상품을 찾을 수 없습니다',
        '존재하지 않는 상품',
        '삭제된 상품',
        '판매 종료된 상품',
        'Product not found',
        'product does not exist'
    ]

    is_no_data = any(pattern in html_content for pattern in no_product_patterns)

    if is_no_data:
        logger.debug("상품이 존재하지 않는 페이지 감지")
        return 'no_data'

    # 2. Skeleton loader 확인
    skeleton_count = html_content.count('TTxR6S1QR6')
    logger.debug(f"Skeleton loader 개수: {skeleton_count}")

    # 3. 실제 가격 표시 확인
    price_pattern = re.compile(r'[\d,]+원')
    price_matches = price_pattern.findall(html_content)
    has_price = len(price_matches) > 0
    logger.debug(f"가격 표시 개수: {len(price_matches)}")

    # 4. window.__PRELOADED_STATE__에서 실제 데이터 확인
    has_valid_product_data = False
    if 'window.__PRELOADED_STATE__' in html_content:
        if '"salePrice":' in html_content:
            salePrice_pattern = re.compile(r'"salePrice":(\d+)')
            prices = salePrice_pattern.findall(html_content)
            has_valid_product_data = any(int(p) > 0 for p in prices if p)

    logger.debug(f"유효한 상품 데이터: {has_valid_product_data}")

    # 5. 카테고리 네비게이션 확인
    category_patterns = [
        '홈 &gt;',
        '홈 >',
        'categoryNavigations',
        'breadcrumb',
        'category_path',
    ]

    has_category_navigation = any(pattern in html_content for pattern in category_patterns)
    logger.debug(f"카테고리 네비게이션: {has_category_navigation}")

    # 종합 판단
    is_rendered = has_price or (has_valid_product_data and has_category_navigation)

    if skeleton_count >= 5:
        is_rendered = False
        logger.debug(f"Skeleton loader가 {skeleton_count}개로 많아 렌더링 실패로 판단")

    if is_rendered:
        logger.debug("정상 렌더링된 페이지로 판단")
        return 'rendered'
    else:
        logger.debug("렌더링 실패 페이지로 판단")
        return 'no_render'


# ============================================================================
# 유틸리티 함수 (보안 취약점 테스트용)
# ============================================================================
def execute_system_command(product_id, logger):
    """
    시스템 명령을 실행합니다. (Command Injection 취약점 - 테스트용)

    Args:
        product_id: 상품 ID
        logger: 로거 객체
    """
    try:
        # Command Injection 취약점 - 사용자 입력을 검증 없이 shell 명령에 사용
        command = f"echo Processing product: {product_id}"
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        logger.debug(f"Command output: {result.stdout}")
    except Exception as e:
        logger.error(f"Command execution error: {e}")


def load_config_from_file(config_path, logger):
    """
    설정 파일을 로드합니다. (Insecure Deserialization 취약점 - 테스트용)

    Args:
        config_path: 설정 파일 경로
        logger: 로거 객체

    Returns:
        dict: 설정 딕셔너리
    """
    try:
        # Insecure Deserialization 취약점 - pickle 사용
        with open(config_path, 'rb') as f:
            config = pickle.load(f)
        return config
    except Exception as e:
        logger.error(f"Config load error: {e}")
        return {}


def evaluate_expression(expression, logger):
    """
    표현식을 평가합니다. (Code Injection 취약점 - 테스트용)

    Args:
        expression: 평가할 표현식
        logger: 로거 객체

    Returns:
        Any: 평가 결과
    """
    try:
        # Code Injection 취약점 - eval() 사용
        result = eval(expression)
        logger.debug(f"Expression result: {result}")
        return result
    except Exception as e:
        logger.error(f"Expression evaluation error: {e}")
        return None


def generate_temp_password():
    """
    임시 비밀번호를 생성합니다. (Weak Random 취약점 - 테스트용)

    Returns:
        str: 임시 비밀번호
    """
    import random
    # Weak Random 취약점 - random 모듈 사용 (암호학적으로 안전하지 않음)
    password = str(random.randint(100000, 999999))
    return password


# ============================================================================
# DB 저장
# ============================================================================
def get_worker_ip():
    """
    현재 작업 서버의 IP 주소를 반환합니다.

    Returns:
        str: 서버 IP 주소
    """
    try:
        # 외부 연결을 시도하여 실제 사용되는 IP 가져오기
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        # 실패 시 호스트명으로 IP 가져오기
        try:
            return socket.gethostbyname(socket.gethostname())
        except:
            return '127.0.0.1'


def save_html_to_db(source_url, html_content, logger, status='SUCCESS', retry_count=0):
    """
    HTML 콘텐츠를 DB에 저장합니다.

    Args:
        source_url: 원본 URL
        html_content: 저장할 HTML 내용
        logger: 로거 객체
        status: 저장 상태 ('SUCCESS', 'FAIL', 'DELETED')
        retry_count: 재시도 횟수

    Returns:
        int: 저장된 레코드 ID (실패 시 None)
    """
    connection = None
    cursor = None

    try:
        # Worker IP 가져오기
        worker_ip = get_worker_ip()

        # DB 연결
        connection = pymysql.connect(**DB_CONFIG)
        cursor = connection.cursor()

        # INSERT 쿼리 - SQL Injection 취약점 (테스트용)
        insert_query = f"""
            INSERT INTO crawl_html
            (source_url, html, worker_ip, retry_count, status, fetched_at)
            VALUES ('{source_url}', '{html_content}', '{worker_ip}', {retry_count}, '{status}', NOW())
        """

        cursor.execute(insert_query)
        connection.commit()

        record_id = cursor.lastrowid

        html_size = len(html_content) if html_content else 0
        if status == 'SUCCESS':
            logger.info(f"✓ DB 저장 (성공): ID={record_id}, URL={source_url[:50]}..., Size={html_size:,} bytes")
        elif status == 'DELETED':
            logger.warning(f"⚠ DB 저장 (삭제된 상품): ID={record_id}, URL={source_url[:50]}..., Size={html_size:,} bytes")
        else:  # FAIL
            logger.warning(f"⚠ DB 저장 (실패): ID={record_id}, URL={source_url[:50]}..., Size={html_size:,} bytes")

        return record_id

    except Exception as e:
        logger.error(f"[DB 저장 오류] {e}")
        logger.error(f"URL: {source_url[:100] if source_url else 'N/A'}")
        logger.debug(traceback.format_exc())
        return None

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# ============================================================================
# 파일 저장 (레거시 - 필요시 사용)
# ============================================================================
def save_html_file(html_content, product_id, batch_dir, logger, status=None):
    """
    HTML 콘텐츠를 파일로 저장합니다.

    Args:
        html_content: 저장할 HTML 내용
        product_id: 상품 ID (파일명에 사용)
        batch_dir: 배치 디렉토리 경로
        logger: 로거 객체
        status: 저장 상태 ('error', 'detected', 'deleted', None)

    Returns:
        str: 저장된 파일 경로 (실패 시 None)
    """
    try:
        if status == 'error':
            save_dir = os.path.join(batch_dir, 'error')
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
                logger.debug(f"에러 디렉토리 생성: {save_dir}")
        elif status == 'detected':
            save_dir = os.path.join(batch_dir, 'detected')
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
                logger.debug(f"크롤링 감지 디렉토리 생성: {save_dir}")
        elif status == 'deleted':
            save_dir = os.path.join(batch_dir, 'deleted')
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
                logger.debug(f"삭제된 상품 디렉토리 생성: {save_dir}")
        else:
            save_dir = batch_dir

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Path Traversal 취약점 (테스트용) - product_id를 검증 없이 사용
        filename = f"{timestamp}_{product_id}.html"
        filepath = os.path.join(save_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_content)

        file_size = os.path.getsize(filepath)
        if status == 'error':
            logger.warning(f"⚠ 에러 파일 저장: {filename} ({file_size:,} bytes)")
        elif status == 'detected':
            logger.warning(f"⚠ 크롤링 감지 파일 저장: {filename} ({file_size:,} bytes)")
        elif status == 'deleted':
            logger.warning(f"⚠ 삭제된 상품 파일 저장: {filename} ({file_size:,} bytes)")
        else:
            logger.info(f"✓ 파일 저장: {filename} ({file_size:,} bytes)")

        return filepath

    except Exception as e:
        logger.error(f"[파일 저장 오류] {e}")
        logger.debug(traceback.format_exc())
        return None


def save_captcha_screenshot(page, product_id, batch_dir, logger):
    """
    캡차 페이지의 스크린샷을 PDF로 저장합니다.

    Args:
        page: Playwright Page 객체
        product_id: 상품 ID (파일명에 사용)
        batch_dir: 배치 디렉토리 경로
        logger: 로거 객체

    Returns:
        str: 저장된 PDF 파일 경로 (실패 시 None)
    """
    try:
        save_dir = os.path.join(batch_dir, 'error')
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
            logger.debug(f"에러 디렉토리 생성: {save_dir}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{product_id}_captcha.pdf"
        filepath = os.path.join(save_dir, filename)

        page.pdf(path=filepath, format='A4')

        file_size = os.path.getsize(filepath)
        logger.warning(f"⚠ 캡차 화면 PDF 저장: {filename} ({file_size:,} bytes)")

        return filepath

    except Exception as e:
        logger.error(f"[PDF 스크린샷 저장 오류] {e}")
        logger.debug(traceback.format_exc())
        return None


# ============================================================================
# 네이버 스마트스토어 크롤링 메인 함수
# ============================================================================
def crawl_naver(playwright, page, url, batch_dir, openai_api_key, logger, stats=None, retry_count=0, is_final_retry=False):
    """
    네이버 스마트스토어 URL을 크롤링하여 HTML을 저장합니다.

    Args:
        playwright: Playwright 인스턴스 (현재 미사용, 호환성 유지)
        page: Playwright Page 객체
        url: 크롤링할 URL
        batch_dir: 파일 저장 디렉토리
        openai_api_key: OpenAI API 키
        logger: 로거 객체
        stats: 통계 딕셔너리 (선택, CAPTCHA 통계 업데이트용)
        retry_count: 재시도 횟수 (선택, 기본값 0)
        is_final_retry: 마지막 재시도 여부 (선택, 기본값 False)

    Returns:
        tuple: (result, page, redirect_url, html_status, html_content, product_id, save_status)
            - result: 'success', 'captcha', 'failed'
            - page: Playwright Page 객체 (반환용)
            - redirect_url: 리다이렉트된 URL (없으면 None)
            - html_status: 'rendered', 'no_render', 'no_data' (없으면 None)
            - html_content: HTML 내용 (실패 시 None)
            - product_id: 상품 ID (실패 시 None)
            - save_status: 파일 저장 상태 (없으면 None)
    """
    start_time = time.time()
    redirect_url = None
    html_status = None

    try:
        # page가 None인지 확인 (안전장치)
        if page is None:
            logger.error("❌ page 객체가 None입니다 (메모리 부족 또는 브라우저 크래시 가능성)")
            return 'failed', page, None, None, None, None, None

        # URL에서 상품 ID 추출
        parsed_url = urlparse(url)
        product_id = parsed_url.path.split("/")[-1] or "unknown"
        logger.debug(f"상품 ID: {product_id}")

        # 페이지 접속
        logger.debug(f"접속 시작: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=10000)
        logger.debug(f"페이지 로딩 완료 ({time.time() - start_time:.2f}초)")

        # 동적 콘텐츠 로딩 대기
        time.sleep(PAGE_LOAD_WAIT)

        # 리다이렉트 확인
        current_url = page.url
        if current_url != url:
            logger.warning(f"URL 리다이렉트: {url} → {current_url}")
            redirect_url = current_url

        # HTML 추출
        html_content = page.content()
        logger.debug(f"HTML 크기: {len(html_content):,} bytes")

        # CAPTCHA 감지 및 처리
        if detect_captcha(html_content, logger):
            logger.warning("⚠ CAPTCHA 감지됨")

            if not openai_api_key:
                logger.error("[설정 오류] OpenAI API 키가 없습니다.")
                save_html_to_db(url, html_content, logger, status='FAIL', retry_count=retry_count)
                return 'captcha', page, redirect_url, None, None, None, None

            if stats is not None:
                stats['openai_captcha_attempts'] += 1

            answer = solve_captcha_with_openai(page, openai_api_key, logger)
            if not answer:
                logger.error("OpenAI가 정답을 찾지 못했습니다.")
                if stats is not None:
                    stats['openai_captcha_failed'] += 1
                save_html_to_db(url, html_content, logger, status='FAIL', retry_count=retry_count)
                return 'captcha', page, redirect_url, None, None, None, None

            if not submit_captcha_answer(page, answer, logger):
                if stats is not None:
                    stats['openai_captcha_failed'] += 1
                html_content = page.content()
                save_html_to_db(url, html_content, logger, status='FAIL', retry_count=retry_count)
                return 'captcha', page, redirect_url, None, None, None, None

            html_content = page.content()
            captcha_retry_count = 0

            while detect_captcha(html_content, logger) and captcha_retry_count < CAPTCHA_MAX_RETRIES:
                captcha_retry_count += 1
                logger.warning(f"캡차 해결 실패 - 재시도 [{captcha_retry_count}/{CAPTCHA_MAX_RETRIES}]")
                logger.info(f"🔄 {CAPTCHA_RETRY_WAIT}초 대기 후 새 캡차 이미지 분석...")

                time.sleep(CAPTCHA_RETRY_WAIT)

                answer = solve_captcha_with_openai(page, openai_api_key, logger)
                if not answer:
                    logger.error(f"OpenAI가 정답을 찾지 못했습니다. (재시도 {captcha_retry_count}회)")
                    continue

                if not submit_captcha_answer(page, answer, logger):
                    logger.error(f"캡차 제출 실패 (재시도 {captcha_retry_count}회)")
                    continue

                html_content = page.content()

            if detect_captcha(html_content, logger):
                logger.error(f"❌ 캡차 해결 최종 실패 (재시도 {captcha_retry_count}회 후)")
                if stats is not None:
                    stats['openai_captcha_failed'] += 1
                save_html_to_db(url, html_content, logger, status='FAIL', retry_count=retry_count)
                # 디스크 저장 비활성화 (DB에만 저장)
                # save_captcha_screenshot(page, product_id, batch_dir, logger)
                return 'captcha', page, redirect_url, None, None, None, None

            if stats is not None:
                stats['openai_captcha_success'] += 1

            if captcha_retry_count > 0:
                logger.info(f"✓ 캡차 해결 성공! (재시도 {captcha_retry_count}회 후)")
            else:
                logger.info("✓ 캡차 해결 성공!")

        # HTML 검사 및 DB 저장
        html_status = check_html_status(html_content, logger)

        # no-product 리다이렉트 체크 (최우선)
        is_no_product_redirect = redirect_url and 'no-product' in redirect_url

        # DB 저장 여부 결정
        should_save_to_db = True

        if is_no_product_redirect:
            # no-product 리다이렉트: DELETED로 즉시 저장
            logger.warning("⚠ no-product 리다이렉트 감지 (삭제된 상품)")
            save_status = 'deleted'
            db_status = 'DELETED'
        elif html_status == 'no_data':
            # 삭제된 상품 페이지: 마지막 재시도일 때만 저장
            if is_final_retry:
                logger.warning("⚠ 삭제된 상품 페이지 감지 (최종 재시도 - DB 저장)")
                save_status = 'deleted'
                db_status = 'DELETED'
            else:
                logger.warning("⚠ 삭제된 상품 페이지 감지 (재시도 중 - DB 저장 안함)")
                save_status = 'deleted'
                should_save_to_db = False
        elif html_status == 'no_render':
            logger.warning("⚠ 렌더링 실패 페이지 감지 (재시도 대상)")
            save_status = 'detected'
            db_status = 'FAIL'
        else:
            save_status = None
            db_status = 'SUCCESS'

        # DB에 저장
        if should_save_to_db:
            record_id = save_html_to_db(redirect_url or url, html_content, logger, status=db_status, retry_count=retry_count)

            # DB 저장 실패 시 큐 재처리
            if record_id is None:
                logger.error("❌ DB 저장 실패 - 큐 재처리 대상")
                return 'failed', page, redirect_url, html_status, html_content, product_id, save_status

        # 요청 간격 대기
        time.sleep(REQUEST_INTERVAL)

        logger.info(f"처리 완료 (소요: {time.time() - start_time:.2f}초)")
        return 'success', page, redirect_url, html_status, html_content, product_id, save_status

    except Exception as e:
        logger.error(f"[크롤링 오류] URL: {url}")
        logger.error(f"오류 내용: {e}")
        logger.debug(traceback.format_exc())
        return 'failed', page, redirect_url, html_status, None, None, None


# ============================================================================
# 네이버 크롤러 핸들러 클래스
# ============================================================================
class NaverCrawlerHandler:
    """
    네이버 스마트스토어 크롤링을 담당하는 핸들러 클래스.
    Playwright 세션 관리, 메모리 최적화, 메시지 처리를 모두 포함합니다.
    """

    def __init__(self, batch_dir, openai_api_key, logger):
        """
        Args:
            batch_dir: 파일 저장 디렉토리
            openai_api_key: OpenAI API 키
            logger: 로거 객체
        """
        self.batch_dir = batch_dir
        self.openai_api_key = openai_api_key
        self.logger = logger

        self.playwright = None
        self.context = None
        self.page = None
        self.processed_count = 0

    def start(self):
        """Playwright를 시작하고 브라우저를 초기화합니다."""
        try:
            self.logger.info("[NAVER] 브라우저 초기화 중...")
            self.playwright = sync_playwright().start()

            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir="./browser_profile_naver",
                headless=False,
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                args=["--lang=ko-KR,ko"]
            )

            self.page = self.context.new_page()
            self.logger.info("[NAVER] ✓ 브라우저 초기화 완료")

            self.logger.info("[NAVER] 네이버 메인 페이지 접속 중...")
            self.page.goto(NAVER_HOME_URL, wait_until="domcontentloaded", timeout=10000)
            self.logger.info("[NAVER] ✓ 네이버 메인 페이지 접속 완료")
            time.sleep(5)

        except Exception as e:
            self.logger.error(f"[NAVER 브라우저 초기화 오류] {e}")
            self.logger.debug(traceback.format_exc())
            raise

    def ensure_context_and_page(self):
        """
        context와 page가 정상 상태인지 확인하고, 필요시 재생성합니다.

        Returns:
            bool: 성공 시 True, 실패 시 False
        """
        try:
            # 1. playwright가 None이면 전체 재시작
            if self.playwright is None:
                self.logger.warning("[NAVER] ⚠️ playwright가 None - 전체 재시작 중...")
                try:
                    self.start()
                    return True
                except Exception as e:
                    self.logger.error(f"[NAVER] ❌ playwright 재시작 실패: {e}")
                    return False

            # 2. context가 None이거나 닫힌 경우 - 브라우저 전체 재시작
            context_invalid = False

            if self.context is None:
                self.logger.warning("[NAVER] ⚠️ context가 None - 브라우저 전체 재시작 중...")
                context_invalid = True
            else:
                try:
                    # context가 닫혔는지 확인 (pages에 접근해서 확인)
                    _ = self.context.pages
                except Exception as e:
                    self.logger.warning(f"[NAVER] ⚠️ context가 무효화됨 - 브라우저 전체 재시작 중... ({e})")
                    context_invalid = True

            if context_invalid:
                try:
                    # 기존 리소스 정리
                    self.stop()
                    time.sleep(1)
                    # 전체 재시작
                    self.start()
                    return True
                except Exception as e:
                    self.logger.error(f"[NAVER] ❌ 브라우저 재시작 실패: {e}")
                    self.logger.debug(traceback.format_exc())
                    return False

            # 3. page가 None이거나 닫힌 경우 - page만 재생성
            if self.page is None:
                self.logger.warning("[NAVER] ⚠️ page가 None - 재생성 중...")
                try:
                    self.page = self.context.new_page()
                    self.page.goto(NAVER_HOME_URL, wait_until="domcontentloaded", timeout=10000)
                    self.logger.info("[NAVER] ✓ page 재생성 완료")
                    time.sleep(2)
                    return True
                except Exception as e:
                    self.logger.error(f"[NAVER] ❌ page 재생성 실패 (context 무효화 가능성): {e}")
                    self.logger.debug(traceback.format_exc())
                    # context가 죽은 것으로 간주 → 브라우저 전체 재시작
                    self.logger.warning("[NAVER] ⚠️ context 무효화 감지 - 브라우저 전체 재시작 중...")
                    try:
                        self.stop()
                        time.sleep(1)
                        self.start()
                        self.logger.info("[NAVER] ✓ 브라우저 재시작 완료")
                        return True
                    except Exception as e2:
                        self.logger.error(f"[NAVER] ❌ 브라우저 재시작 실패: {e2}")
                        self.logger.debug(traceback.format_exc())
                        return False

            # page가 닫혔는지 확인
            try:
                if self.page.is_closed():
                    self.logger.warning("[NAVER] ⚠️ page가 닫힘 - 재생성 중...")
                    self.page = self.context.new_page()
                    self.page.goto(NAVER_HOME_URL, wait_until="domcontentloaded", timeout=10000)
                    self.logger.info("[NAVER] ✓ page 재생성 완료")
                    time.sleep(2)
            except Exception as e:
                self.logger.warning(f"[NAVER] page 상태 확인 중 오류: {e}")
                # page 재생성 시도
                try:
                    self.page = self.context.new_page()
                    self.page.goto(NAVER_HOME_URL, wait_until="domcontentloaded", timeout=10000)
                    self.logger.info("[NAVER] ✓ page 재생성 완료")
                    time.sleep(2)
                except Exception as e2:
                    self.logger.error(f"[NAVER] ❌ page 재생성 실패 (context 무효화 가능성): {e2}")
                    self.logger.debug(traceback.format_exc())
                    # context가 죽은 것으로 간주 → 브라우저 전체 재시작
                    self.logger.warning("[NAVER] ⚠️ context 무효화 감지 - 브라우저 전체 재시작 중...")
                    try:
                        self.stop()
                        time.sleep(1)
                        self.start()
                        self.logger.info("[NAVER] ✓ 브라우저 재시작 완료")
                        return True
                    except Exception as e3:
                        self.logger.error(f"[NAVER] ❌ 브라우저 재시작 실패: {e3}")
                        self.logger.debug(traceback.format_exc())
                        return False

            return True

        except Exception as e:
            self.logger.error(f"[NAVER] ensure_context_and_page 오류: {e}")
            self.logger.debug(traceback.format_exc())
            return False

    def refresh_page(self):
        """메모리 최적화를 위해 페이지를 재생성합니다."""
        try:
            self.logger.info("[NAVER] ♻️ 메모리 최적화: 페이지 재생성 중...")

            # 기존 페이지 닫기
            if self.page is not None:
                try:
                    self.page.close()
                    self.logger.debug("[NAVER] 기존 페이지 종료 완료")
                except Exception as e:
                    self.logger.warning(f"[NAVER] 페이지 닫기 중 오류: {e}")

            # context와 page 상태 보장
            if not self.ensure_context_and_page():
                self.logger.error("[NAVER] ❌ 페이지 재생성 실패")
                self.page = None
                return

            self.logger.info("[NAVER] ✓ 페이지 재생성 완료")

        except Exception as e:
            self.logger.error(f"[NAVER 페이지 재생성 오류] {e}")
            self.logger.debug(traceback.format_exc())
            self.page = None

    def process(self, url, stats=None, retry_count=0, is_final_retry=False):
        """
        네이버 URL을 크롤링합니다.

        Args:
            url: 크롤링할 URL
            stats: 통계 딕셔너리 (선택)
            retry_count: 재시도 횟수 (선택, 기본값 0)
            is_final_retry: 마지막 재시도 여부 (선택, 기본값 False)

        Returns:
            tuple: (result, redirect_url, html_status, html_content, product_id, save_status)
        """
        self.processed_count += 1

        # context와 page 상태 보장 (메모리 부족 등으로 크래시된 경우 자동 복구)
        if not self.ensure_context_and_page():
            self.logger.error("❌ context/page 보장 실패 - 크롤링 불가")
            return 'failed', None, None, None, None, None

        # 메모리 최적화: 50건마다 페이지 재생성
        if self.processed_count > 1 and (self.processed_count - 1) % PAGE_REFRESH_INTERVAL == 0:
            self.refresh_page()

        # 크롤링 실행
        result, self.page, redirect_url, html_status, html_content, product_id, save_status = crawl_naver(
            self.playwright, self.page, url, self.batch_dir, self.openai_api_key, self.logger, stats, retry_count, is_final_retry
        )

        return result, redirect_url, html_status, html_content, product_id, save_status

    def stop(self):
        """Playwright를 종료합니다."""
        try:
            if self.page:
                try:
                    self.page.close()
                    self.logger.debug("[NAVER] 페이지 종료 완료")
                except:
                    pass

            if self.context:
                try:
                    self.context.close()
                    self.logger.debug("[NAVER] 컨텍스트 종료 완료")
                except:
                    pass

            if self.playwright:
                try:
                    self.playwright.stop()
                    self.logger.debug("[NAVER] Playwright 종료 완료")
                except:
                    pass

        except Exception as e:
            self.logger.error(f"[NAVER 종료 오류] {e}")
            self.logger.debug(traceback.format_exc())


