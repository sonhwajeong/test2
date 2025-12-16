"""
RabbitMQ Producer
==================
CSV 파일에서 URL을 읽어서 RabbitMQ에 메시지를 전송합니다.
URL에 따라 자동으로 사이트를 감지하여 해당 큐로 전송합니다.

메시지 형식:  
{
    "site": "naver",  # or "coupang"
    "retryCount": 0,
    "url": "https://smartstore.naver.com/...",
    "sleepSeconds": 0
}

지원 사이트:
- naver: https://smartstore.naver.com -> naverQ
- coupang: https://www.coupang.com -> coupangQ
"""

import pika
import csv
import json
import logging
import os

# .env 파일 로드
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================================================================
# 설정
# ============================================================================
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "172.31.11.219")
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "admin")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "a1234")

# 사이트별 큐 매핑
QUEUE_MAPPING = {
    'naver': 'naverQ',
    'coupang': 'coupangQ',
}

CSV_FILE_PATH = "prod_urls_server3.csv"
TEST_LIMIT = None  # None이면 전체, 숫자를 입력하면 해당 개수만 전송


# ============================================================================
# 로깅 설정
# ============================================================================
def setup_logging():
    """로깅 환경을 설정합니다."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%H:%M:%S'
    )
    return logging.getLogger("QueueProducer")


# ============================================================================
# 사이트 감지
# ============================================================================
def detect_site(url):
    """
    URL에서 사이트를 자동 감지합니다.

    Args:
        url: 확인할 URL

    Returns:
        str: 'naver', 'coupang', 또는 None
    """
    if 'smartstore.naver.com' in url:
        return 'naver'
    elif 'coupang.com' in url:
        return 'coupang'
    return None


# ============================================================================
# CSV 파일 읽기
# ============================================================================
def read_urls_from_csv(csv_path, logger):
    """
    CSV 파일에서 n_url 컬럼을 읽어 URL 및 사이트 정보를 반환합니다.

    Args:
        csv_path: CSV 파일 경로
        logger: 로거 객체

    Returns:
        list: [{'site': 'naver', 'url': 'https://...'}, ...] 형태의 리스트
    """
    url_items = []

    try:
        logger.debug(f"CSV 파일 열기: {csv_path}")

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            # n_url 컬럼 존재 확인
            if 'n_url' not in reader.fieldnames:
                logger.error("[CSV 오류] 'n_url' 컬럼이 없습니다.")
                return []

            # 각 행에서 URL 추출
            for row_num, row in enumerate(reader, start=1):
                url = row.get('n_url', '').strip()

                # 빈 URL 건너뛰기
                if not url:
                    logger.debug(f"행 {row_num}: 빈 URL (건너뜀)")
                    continue

                # URL에서 사이트 자동 감지
                site = detect_site(url)

                if site:
                    url_items.append({'site': site, 'url': url})
                else:
                    logger.warning(f"행 {row_num}: 지원하지 않는 URL - {url}")

        # 사이트별 통계
        site_counts = {}
        for item in url_items:
            site_counts[item['site']] = site_counts.get(item['site'], 0) + 1

        logger.info(f"총 {len(url_items)}개의 유효한 URL을 읽었습니다.")
        for site, count in site_counts.items():
            logger.info(f"  - {site}: {count}개")

        return url_items

    except FileNotFoundError:
        logger.error(f"[파일 오류] CSV 파일을 찾을 수 없습니다: {csv_path}")
        return []
    except Exception as e:
        logger.error(f"[CSV 읽기 오류] {e}")
        return []


# ============================================================================
# RabbitMQ에 메시지 전송
# ============================================================================
def send_messages_to_queue(url_items, logger):
    """
    URL 리스트를 RabbitMQ에 메시지로 전송합니다.
    각 URL의 사이트에 따라 적절한 큐로 라우팅됩니다.

    Args:
        url_items: [{'site': 'naver', 'url': 'https://...'}, ...] 형태의 리스트
        logger: 로거 객체
    """
    try:
        # RabbitMQ 연결
        logger.info(f"RabbitMQ 서버 연결 중: {RABBITMQ_HOST}")

        # 인증 정보 설정
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)

        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=RABBITMQ_HOST,
                credentials=credentials
            )
        )
        channel = connection.channel()

        # 사용될 모든 큐 선언 (존재하지 않으면 생성, durable=True로 영구 저장)
        declared_queues = set()
        for site in QUEUE_MAPPING.keys():
            queue_name = QUEUE_MAPPING[site]
            channel.queue_declare(queue=queue_name, durable=True)
            declared_queues.add(queue_name)
            logger.info(f"✓ 큐 선언 완료: {queue_name}")

        # 각 URL을 메시지로 전송
        logger.info("-" * 70)
        sent_counts = {}  # 사이트별 전송 개수

        for idx, item in enumerate(url_items, start=1):
            site = item['site']
            url = item['url']
            queue_name = QUEUE_MAPPING.get(site)

            if not queue_name:
                logger.error(f"[{idx}] 알 수 없는 사이트: {site}")
                continue

            message = {
                'site': site,
                'retryCount': 0,
                'url': url,
                'sleepSeconds': 0
            }

            channel.basic_publish(
                exchange='',
                routing_key=queue_name,
                body=json.dumps(message),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # 메시지를 영구 저장
                )
            )

            # 사이트별 카운트
            sent_counts[site] = sent_counts.get(site, 0) + 1

            if idx % 100 == 0 or idx == len(url_items):
                logger.info(f"[{idx}/{len(url_items)}] 메시지 전송 중...")

        connection.close()
        logger.info("-" * 70)
        logger.info(f"✓ 총 {len(url_items)}개의 메시지를 큐에 전송했습니다.")
        for site, count in sent_counts.items():
            logger.info(f"  - {QUEUE_MAPPING[site]}: {count}개")

    except pika.exceptions.AMQPConnectionError as e:
        logger.error(f"[RabbitMQ 연결 오류] {RABBITMQ_HOST}에 연결할 수 없습니다: {e}")
    except Exception as e:
        logger.error(f"[RabbitMQ 오류] {e}")


# ============================================================================
# 메인 함수
# ============================================================================
def main():
    """Producer 메인 함수"""
    logger = setup_logging()

    logger.info("=" * 70)
    logger.info("RabbitMQ Producer 시작")
    logger.info("=" * 70)
    logger.info(f"RabbitMQ 서버: {RABBITMQ_HOST}")
    logger.info(f"지원 큐: {', '.join(QUEUE_MAPPING.values())}")
    logger.info(f"CSV 파일: {CSV_FILE_PATH}")
    if TEST_LIMIT:
        logger.warning(f"테스트 모드: {TEST_LIMIT}개만 전송")
    logger.info("-" * 70)

    # CSV에서 URL 읽기
    url_items = read_urls_from_csv(CSV_FILE_PATH, logger)

    if not url_items:
        logger.error("전송할 URL이 없습니다. 종료합니다.")
        return

    # 테스트 모드 적용
    if TEST_LIMIT:
        url_items = url_items[:TEST_LIMIT]
        logger.info(f"테스트 모드: {len(url_items)}개만 전송합니다.")

    # RabbitMQ에 메시지 전송
    send_messages_to_queue(url_items, logger)

    logger.info("=" * 70)
    logger.info("Producer 작업 완료")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()

