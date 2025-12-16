"""
RabbitMQ Consumer (Listener)
============================
RabbitMQ에서 메시지를 소비하고 사이트별 크롤링을 수행합니다.
- naverQ: 네이버 스마트스토어 크롤링
- coupangQ: 쿠팡 크롤링

메시지 형식:
{
    "site": "naver",  # or "coupang"
    "retryCount": 0,
    "url": "https://smartstore.naver.com/...",
    "sleepSeconds": 0
}
"""

import pika
import json
import logging
import time
import os
import traceback
from datetime import datetime
from urllib.parse import urlparse

# 사이트별 크롤러 핸들러 import
from crawler_naver import NaverCrawlerHandler  # save_html_file 제거 (DB에만 저장)
from crawler_coupang import CoupangCrawlerHandler

# .env 파일 로드
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================================================================
# 설정 및 상수
# ============================================================================
# RabbitMQ 설정
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "172.31.11.219")
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "admin")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "a1234")

# 큐 설정 (여러 큐 지원)
QUEUES = ["naverQ", "coupangQ"]  # 리스닝할 큐 목록

MAX_RETRY_COUNT = 3  # 최대 재처리 횟수
RETRY_SLEEP_SECONDS = 180  # 재처리 2회차부터 대기 시간 (초)

# 파일 경로
OUTPUT_DIR = "output"
LOG_DIR = "logs"

# OpenAI API 키
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# ============================================================================
# 로깅 설정
# ============================================================================
def setup_logging(start_time):
    """로깅 환경을 설정하고 logger 객체를 반환합니다."""
    year = start_time.strftime("%Y")
    month = start_time.strftime("%m")

    log_subdir = os.path.join(LOG_DIR, year, month)
    if not os.path.exists(log_subdir):
        os.makedirs(log_subdir)

    timestamp = start_time.strftime("%Y%m%d_%H%M%S")
    log_filename = os.path.join(log_subdir, f"crawler_{timestamp}.log")

    logger = logging.getLogger("NaverCrawler")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        logger.handlers.clear()

    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(funcName)-25s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%H:%M:%S'
    ))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    logger.info(f"로그 파일 생성: {log_filename}")
    return logger


def create_batch_directory(start_time, logger):
    """크롤링 배치를 위한 출력 디렉토리를 생성합니다."""
    year = start_time.strftime("%Y")
    month = start_time.strftime("%m")
    day = start_time.strftime("%d")
    batch_id = start_time.strftime("%H%M%S")

    batch_dir = os.path.join(OUTPUT_DIR, year, month, day, batch_id)

    if not os.path.exists(batch_dir):
        os.makedirs(batch_dir)
        logger.info(f"배치 디렉토리 생성: {batch_dir}")
    else:
        logger.warning(f"배치 디렉토리 이미 존재: {batch_dir}")

    return batch_dir


# ============================================================================
# Consumer 클래스
# ============================================================================
class crawlerConsumer:
    """RabbitMQ 메시지를 소비하고 크롤링을 수행하는 Consumer 클래스"""

    def __init__(self, logger, batch_dir):
        self.logger = logger
        self.connection = None
        self.channel = None
        self.batch_dir = batch_dir

        # 사이트별 크롤러 핸들러 (Lazy Loading)
        self.naver_handler = None
        self.coupang_handler = None

        self.stats = {
            'total_processed': 0,
            'success': 0,
            'captcha': 0,
            'failed': 0,
            'retry_sent': 0,
            'max_retry_exceeded': 0,
            'openai_captcha_attempts': 0,
            'openai_captcha_success': 0,
            'openai_captcha_failed': 0,
            'start_time': datetime.now()
        }
        self.processed_count = 0

    def connect_rabbitmq(self):
        """RabbitMQ에 연결하고 여러 큐를 선언합니다."""
        try:
            self.logger.info(f"RabbitMQ 서버 연결 중: {RABBITMQ_HOST}")

            # 인증 정보 설정
            credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)

            self.connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=RABBITMQ_HOST,
                    credentials=credentials
                )
            )
            self.channel = self.connection.channel()

            # 여러 큐 선언
            for queue in QUEUES:
                self.channel.queue_declare(queue=queue, durable=True)
                self.logger.info(f"✓ 큐 선언 완료: {queue}")

            self.channel.basic_qos(prefetch_count=1)

        except Exception as e:
            self.logger.error(f"[RabbitMQ 연결 오류] {e}")
            self.logger.debug(traceback.format_exc())
            raise

    def send_retry_message(self, site, url, retry_count, queue_name):
        """재처리를 위해 메시지를 다시 큐에 전송합니다."""
        try:
            new_retry_count = retry_count + 1
            sleep_seconds = RETRY_SLEEP_SECONDS if new_retry_count >= 2 else 0

            message = {
                'site': site,  # 사이트 정보 포함
                'retryCount': new_retry_count,
                'url': url,
                'sleepSeconds': sleep_seconds
            }

            self.channel.basic_publish(
                exchange='',
                routing_key=queue_name,  # 원래 큐로 다시 전송
                body=json.dumps(message),
                properties=pika.BasicProperties(delivery_mode=2)
            )

            parsed = urlparse(url)
            product_id = parsed.path.split("/")[-1] or "unknown"
            self.logger.info(f"📝 재처리 메시지 전송 [{site}]: {product_id} (다음 재처리: {new_retry_count}회차, sleep: {sleep_seconds}초)")
            self.stats['retry_sent'] += 1

        except Exception as e:
            self.logger.error(f"[재처리 메시지 전송 오류] {e}")
            self.logger.debug(traceback.format_exc())

    def process_message(self, ch, method, properties, body):
        """RabbitMQ 메시지를 처리합니다."""
        try:
            message = json.loads(body)
            site = message.get('site', 'naver')  # 기본값: naver (하위 호환성)
            url = message.get('url')
            retry_count = message.get('retryCount', 0)
            sleep_seconds = message.get('sleepSeconds', 0)
            queue_name = method.routing_key  # 메시지가 온 큐 이름

            self.stats['total_processed'] += 1
            self.processed_count += 1

            parsed = urlparse(url)
            product_id = parsed.path.split("/")[-1] or "unknown"

            self.logger.info("=" * 70)
            if retry_count > 0:
                self.logger.info(f"[처리 {self.stats['total_processed']}] [{site.upper()}] 재처리 {retry_count}회차 - {product_id}")
            else:
                self.logger.info(f"[처리 {self.stats['total_processed']}] [{site.upper()}] 신규 - {product_id}")
            self.logger.info(f"Queue: {queue_name}")
            self.logger.info(f"URL: {url}")

            if sleep_seconds > 0:
                self.logger.info(f"⏰ {sleep_seconds}초 대기 중...")
                time.sleep(sleep_seconds)

            # 마지막 재시도 여부 확인
            is_final_retry = (retry_count >= MAX_RETRY_COUNT)

            # 사이트별 분기 처리
            if site == 'naver':
                # 네이버 핸들러가 없으면 생성 및 시작
                if self.naver_handler is None:
                    self.naver_handler = NaverCrawlerHandler(self.batch_dir, OPENAI_API_KEY, self.logger)
                    self.naver_handler.start()

                # 네이버 크롤링 실행 (핸들러가 메모리 최적화도 담당)
                result, redirect_url, html_status, html_content, product_id, save_status = self.naver_handler.process(
                    url, self.stats, retry_count, is_final_retry
                )

            elif site == 'coupang':
                # 쿠팡 핸들러가 없으면 생성 및 시작
                if self.coupang_handler is None:
                    self.coupang_handler = CoupangCrawlerHandler(self.batch_dir, OPENAI_API_KEY, self.logger)
                    self.coupang_handler.start()

                # 쿠팡 크롤링 실행 (핸들러가 메모리 최적화도 담당)
                result, redirect_url, html_status, html_content, product_id, save_status = self.coupang_handler.process(
                    url, self.stats
                )

            else:
                self.logger.error(f"지원하지 않는 사이트: {site}")
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return

            if result == 'success':
                self.stats['success'] += 1
                self.logger.info(f"✓ 성공")
            elif result == 'captcha':
                self.stats['captcha'] += 1
                self.logger.warning(f"⚠ CAPTCHA")
            else:
                self.stats['failed'] += 1
                self.logger.error(f"✗ 실패")

            # 재처리 로직
            should_retry = False
            retry_reason = None

            if html_status == 'no_render':
                should_retry = True
                retry_reason = 'no_render'
            elif html_status == 'no_data':
                if not (redirect_url and 'no-product' in redirect_url):
                    should_retry = True
                    retry_reason = 'no_data(렌더링 실패 추정)'

            if should_retry:
                if redirect_url and 'no-product' in redirect_url:
                    self.logger.info(f"⏭️ no-product 리다이렉트 - 재처리 제외")
                    # 디스크 저장 비활성화 (DB에만 저장)
                    # if html_content and product_id:
                    #     save_html_file(html_content, product_id, self.batch_dir, self.logger, status=save_status)
                elif retry_count >= MAX_RETRY_COUNT:
                    self.logger.warning(f"⚠ 최대 재처리 횟수 초과 ({retry_count}회) - 더 이상 재처리하지 않음")
                    self.stats['max_retry_exceeded'] += 1
                    # 디스크 저장 비활성화 (DB에만 저장)
                    # if html_content and product_id:
                    #     save_html_file(html_content, product_id, self.batch_dir, self.logger, status=save_status)
                else:
                    self.send_retry_message(site, url, retry_count, queue_name)
            else:
                # 디스크 저장 비활성화 (DB에만 저장)
                # if html_content and product_id:
                #     save_html_file(html_content, product_id, self.batch_dir, self.logger, status=save_status)
                pass

            ch.basic_ack(delivery_tag=method.delivery_tag)
            self.logger.info("-" * 70)

        except json.JSONDecodeError as e:
            self.logger.error(f"[메시지 파싱 오류] {e}")
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as e:
            self.logger.error(f"[메시지 처리 오류] {e}")
            self.logger.debug(traceback.format_exc())
            ch.basic_ack(delivery_tag=method.delivery_tag)

    def start_consuming(self):
        """여러 큐에서 메시지 소비를 시작합니다."""
        try:
            self.logger.info("=" * 70)
            self.logger.info("메시지 수신 대기 중...")
            self.logger.info(f"리스닝 큐: {', '.join(QUEUES)}")
            self.logger.info("종료하려면 Ctrl+C를 누르세요")
            self.logger.info("=" * 70)

            # 여러 큐를 동시에 리스닝
            for queue in QUEUES:
                self.channel.basic_consume(
                    queue=queue,
                    on_message_callback=self.process_message
                )

            self.channel.start_consuming()

        except KeyboardInterrupt:
            self.logger.info("\n사용자에 의해 중단되었습니다.")
            self.stop()
        except Exception as e:
            self.logger.error(f"[소비 오류] {e}")
            self.logger.debug(traceback.format_exc())
            self.stop()

    def stop(self):
        """Consumer를 종료합니다."""
        try:
            self.logger.info("=" * 70)
            self.logger.info("Consumer 종료 중...")

            if self.channel and self.channel.is_open:
                self.channel.stop_consuming()
                self.logger.debug("채널 종료 완료")

            if self.connection and self.connection.is_open:
                self.connection.close()
                self.logger.debug("RabbitMQ 연결 종료 완료")

            # 사이트별 핸들러 종료
            if self.naver_handler:
                try:
                    self.naver_handler.stop()
                except Exception as e:
                    self.logger.warning(f"[NAVER] 핸들러 종료 중 오류: {e}")

            if self.coupang_handler:
                try:
                    self.coupang_handler.stop()
                except Exception as e:
                    self.logger.warning(f"[COUPANG] 핸들러 종료 중 오류: {e}")

            self.logger.info("모든 크롤러 핸들러 종료 완료")

            self.print_statistics()

        except Exception as e:
            self.logger.error(f"[종료 오류] {e}")
            self.logger.debug(traceback.format_exc())

    def print_statistics(self):
        """통계를 출력합니다."""
        end_time = datetime.now()
        duration = (end_time - self.stats['start_time']).total_seconds()
        duration_minutes = duration / 60

        self.logger.info("=" * 70)
        self.logger.info("최종 통계")
        self.logger.info("=" * 70)
        self.logger.info(f"총 처리 건수:           {self.stats['total_processed']}개")
        self.logger.info(f"성공:                   {self.stats['success']}개")
        self.logger.info(f"CAPTCHA 실패:           {self.stats['captcha']}개")
        self.logger.info(f"기타 실패:              {self.stats['failed']}개")
        self.logger.info(f"재처리 메시지 전송:     {self.stats['retry_sent']}개")
        self.logger.info(f"최대 재처리 횟수 초과:  {self.stats['max_retry_exceeded']}개")
        self.logger.info("-" * 70)
        self.logger.info(f"CAPTCHA 시도:           {self.stats['openai_captcha_attempts']}회")
        self.logger.info(f"CAPTCHA 성공:           {self.stats['openai_captcha_success']}회")
        self.logger.info(f"CAPTCHA 실패:           {self.stats['openai_captcha_failed']}회")
        if self.stats['openai_captcha_attempts'] > 0:
            captcha_success_rate = self.stats['openai_captcha_success'] / self.stats['openai_captcha_attempts'] * 100
            self.logger.info(f"CAPTCHA 성공률:         {captcha_success_rate:.1f}%")
        self.logger.info("-" * 70)
        self.logger.info(f"총 소요시간:            {duration_minutes:.2f}분")
        self.logger.info(f"결과 저장:              {self.batch_dir}")
        self.logger.info("=" * 70)


# ============================================================================
# 메인 함수
# ============================================================================
def main():
    """Consumer 메인 함수"""
    start_time = datetime.now()

    logger = setup_logging(start_time)

    logger.info("=" * 70)
    logger.info("RabbitMQ Consumer (Listener) 시작")
    logger.info("=" * 70)
    logger.info(f"RabbitMQ 서버: {RABBITMQ_HOST}")
    logger.info(f"리스닝 큐: {', '.join(QUEUES)}")
    logger.info(f"최대 재처리 횟수: {MAX_RETRY_COUNT}회")
    logger.info(f"재처리 대기 시간: {RETRY_SLEEP_SECONDS}초 (2회차부터)")

    if OPENAI_API_KEY:
        logger.info("✓ OpenAI API 키 확인됨")
    else:
        logger.warning("⚠ OpenAI API 키가 설정되지 않았습니다. CAPTCHA 자동 해결 불가")

    logger.info("-" * 70)

    batch_dir = create_batch_directory(start_time, logger)

    consumer = crawlerConsumer(logger, batch_dir)

    try:
        # 핸들러는 첫 메시지를 받을 때 사이트별로 자동 생성 및 시작
        consumer.connect_rabbitmq()
        consumer.start_consuming()

    except Exception as e:
        logger.critical(f"[치명적 오류] {e}")
        logger.debug(traceback.format_exc())
        consumer.stop()


if __name__ == "__main__":
    main()
