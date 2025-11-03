# 경주신라CC Streamlit 앱 (최종 완료 - 인용 마커 삭제 완료)
import warnings

# RuntimeWarning: coroutine '...' was never awaited 경고를 무시하도록 설정
warnings.filterwarnings(
    "ignore",
    message="coroutine '.*' was never awaited",
    category=RuntimeWarning
)

import streamlit as st
st.set_page_config(
    page_title="경주신라CC 모바일 예약", # 원하는 앱 제목으로 변경
    page_icon="⛳", # 이모지(Emoji)를 사용하거나 아래처럼 이미지 파일을 사용합니다.
    layout="wide", # 앱의 기본 레이아웃을 넓게 설정 (선택 사항)
)
import datetime
import threading
import time
import queue
import sys
import traceback
import requests
import ujson as json
import urllib3
import re
import pytz
# import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime  # For parsing HTTP Date header
from bs4 import BeautifulSoup  # HTML 파싱에 필요

# InsecureRequestWarning 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# KST 시간대 객체 전역 정의
KST = pytz.timezone('Asia/Seoul')


# ============================================================
# Utility Functions
# ============================================================

def log_message(message, message_queue):
    """Logs a message with KST timestamp to the queue."""
    try:
        now_kst = datetime.datetime.now(KST)
        timestamp = now_kst.strftime('%H:%M:%S.%f')[:-3]
        message_queue.put(f"UI_LOG:[{timestamp}] {message}")
    except Exception:
        pass


def get_default_date(days):
    """Gets a default date offset by 'days' from today (KST)."""
    return (datetime.datetime.now(KST).date() + datetime.timedelta(days=days))


def format_time_for_api(time_str):
    """Converts HH:MM to HHMM."""
    if not isinstance(time_str, str): time_str = str(time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{3,4}$', time_str) and time_str.isdigit():
        if len(time_str) == 4:
            return time_str
        elif len(time_str) == 3:
            return f"0{time_str}"
    return "0000"


def format_time_for_display(time_str):
    """Converts HHMM or HH:MM string to HH:MM display format."""
    if not isinstance(time_str, str): time_str = str(time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{4}$', time_str) and time_str.isdigit():
        return f"{time_str[:2]}:{time_str[2:]}"
    # Handle cases where input might already be HH:MM
    if len(time_str) == 5 and time_str[2] == ':':
        return time_str
    return time_str  # Return original if format is unexpected


def wait_until(target_dt_kst, stop_event, message_queue, log_prefix="프로그램 실행", log_countdown=False):
    """
    Waits precisely until the target KST datetime, with a 30-second countdown.
    Logs the countdown if log_countdown is True.
    """
    global KST

    # 1. 초기 계산 및 상태 점검
    now_kst = datetime.datetime.now(KST)
    remaining_seconds = (target_dt_kst - now_kst).total_seconds()
    log_remaining_start = 30  # 카운트다운 시작 기준 시간 (30초)

    log_message(f"⏳ {log_prefix} 대기중: {target_dt_kst.strftime('%H:%M:%S.%f')[:-3]} (KST 기준)", message_queue)

    if remaining_seconds <= 0.001:
        # 이미 시간이 지났거나 도달한 경우 (start_pre_process에서 걸러지지만 안전장치)
        log_message(f"⚠️ 목표 시간이 이미 지났거나 도달했습니다. 즉시 실행.", message_queue)
        return

    # 2. 긴 대기 단계 (30초 이상 남은 경우)
    if log_countdown and remaining_seconds > log_remaining_start:
        time_to_sleep_long = remaining_seconds - log_remaining_start

        log_message(
            f"⏳ {log_prefix} 대기중: {target_dt_kst.strftime('%H:%M:%S')}까지 {remaining_seconds:.1f}초 남음. ({log_remaining_start}초 전부터 카운트다운 시작)",
            message_queue
        )

        # 30초 지점까지 대기
        time.sleep(max(0, time_to_sleep_long))

        if stop_event.is_set():
            log_message("🛑 대기 중 중단 신호 수신.", message_queue)
            return

    # 3. 카운트다운 루프 단계 (30초 이하 남은 경우)
    if log_countdown:
        # 긴 대기 후 남은 시간을 다시 계산
        remaining_seconds = (target_dt_kst - datetime.datetime.now(KST)).total_seconds()
        countdown_start = int(remaining_seconds)

        # 현재 정수 초부터 1초까지 루프 실행
        for seconds_left in range(countdown_start, 0, -1):
            if stop_event.is_set():
                log_message("🛑 대기 중 중단 신호 수신.", message_queue)
                return

            # 사용자 요청 로그 형식: "예약시도 대기중 : ???초"
            log_message(f"⏳ 예약시도 대기중 : {seconds_left}초", message_queue)

            # 다음 정수 초 경계(seconds_left - 1)까지의 정확한 대기 시간 계산
            next_log_time = target_dt_kst - datetime.timedelta(seconds=(seconds_left - 1))
            sleep_duration = (next_log_time - datetime.datetime.now(KST)).total_seconds()

            if sleep_duration > 0:
                time.sleep(sleep_duration)
            else:
                # 시간이 이미 지나 다음 로그 시점을 놓친 경우, 10ms 슬립 후 다음 루프 실행
                time.sleep(0.01)

            # 1초 남았을 때 루프 종료 (밀리초 단위의 최종 대기는 아래에서 처리)
            if seconds_left == 1:
                break

    # 4. 최종 미세 대기 (밀리초 단위 정확성 확보)
    if not stop_event.is_set():
        # 목표 시간까지 남은 최종 시간을 계산
        final_wait = (target_dt_kst - datetime.datetime.now(KST)).total_seconds()

        if final_wait > 0:
            time.sleep(final_wait)

        # 5. 실행 완료 로그
        actual_diff = (datetime.datetime.now(KST) - target_dt_kst).total_seconds()
        # ms 단위로 출력
        log_message(f"✅ 목표 시간 도달! {log_prefix} 스레드 즉시 실행. (종료 시각 차이: {actual_diff * 1000:.3f}ms)", message_queue)


# ============================================================
# API Booking Core Class (경주신라CC 전용)
# ============================================================
class APIBookingCore:
    def __init__(self, log_func, message_queue, stop_event):
        self.log_message_func = log_func
        self.message_queue = message_queue
        self.stop_event = stop_event
        self.session = requests.Session()
        self.member_id = None  # Store member_id after login

        # HTML 응답을 기준으로 코스 맵핑 완료
        self.course_detail_mapping = {
            "1": "천마OUT",
            "2": "천마IN",
            "3": "화랑OUT",
            "4": "화랑IN"
        }
        self.proxies = None
        self.KST = pytz.timezone('Asia/Seoul')

        # 핵심 URL 정의 (경주신라CC 기준)
        self.API_DOMAIN = "https://sillacc.co.kr"
        self.LOGIN_URL = f"{self.API_DOMAIN}/member/login"
        self.RESERVATION_PAGE_URL = f"{self.API_DOMAIN}/reservation/golf"
        self.CALENDAR_URL = f"{self.API_DOMAIN}/reservation/ajax/golfCalendar"
        self.TIME_LIST_URL = f"{self.API_DOMAIN}/reservation/ajax/golfTimeList"
        self.BOOK_CHECK_URL = f"{self.API_DOMAIN}/reservation/ajax/golfNoChk"
        self.BOOK_SUBMIT_URL = f"{self.API_DOMAIN}/reservation/ajax/golfSubmit"

    def log_message(self, msg):
        """Logs a message via the provided log function."""
        self.log_message_func(msg, self.message_queue)

    # ----------------------------------------------------
    # 기본 헤더 (경주신라CC 기준)
    # ----------------------------------------------------
    def get_base_headers(self, referer_url):
        """기본 헤더를 반환하는 헬퍼 함수"""
        # 모바일 User-Agent 사용
        return {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Referer": referer_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.API_DOMAIN,
        }

    # ----------------------------------------------------

    # 경주신라CC 로그인 로직
    # 경주신라CC 로그인 로직 (수정됨: 예약 페이지 진입 헤더 강화)
    # 경주신라CC 로그인 로직 (AJAX 기반으로 수정)
    # 경주신라CC 로그인 로직 (AJAX 기반, 로깅 호출 오류 수정)
    def requests_login(self, usrid, usrpass):
        """
        경주신라CC의 AJAX 기반 로그인(`loginChk`)을 수행하고 세션을 안정화합니다.
        """
        self.session = requests.Session()
        self.session.verify = False

        # 1. 로그인 체크 (loginChk) POST 요청 URL
        LOGIN_CHK_URL = "https://sillacc.co.kr/member/loginChk"

        # 로그 분석을 기반으로 AJAX 요청 헤더 설정
        login_headers = self.get_base_headers(self.LOGIN_URL)
        login_headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        login_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        login_headers["X-Requested-With"] = "XMLHttpRequest"
        login_headers["Sec-Fetch-Mode"] = "cors"
        # 일반 요청 헤더 제거
        login_headers.pop("Upgrade-Insecure-Requests", None)

        # 2. 로그인 폼 데이터 (Payload)
        login_data = {
            "returnURL": "/",
            "usrId": usrid,
            "usrPwd": usrpass,
            "rememberId": "Y",
            "rememberPwd": "Y"
        }

        try:
            # 3. 로그인 POST 요청
            res = self.session.post(LOGIN_CHK_URL, headers=login_headers, data=login_data, timeout=10, verify=False,
                                    allow_redirects=False)
            res.raise_for_status()

            # 4. 로그인 성공 확인 (JSON 응답 확인)
            try:
                # AJAX 요청은 200 OK와 JSON을 반환함
                login_response_json = res.json()
                if not login_response_json:
                    # FIX: message_queue 인자 제거
                    self.log_message("⚠️ 로그인 체크 JSON 응답을 수신했으나, 내용 확인 불가. 다음 단계 진행.")

                # FIX: message_queue 인자 제거
                self.log_message("✅ 로그인 POST 성공 (loginChk JSON 응답 수신).")
            except json.JSONDecodeError:
                # FIX: message_queue 인자 제거
                self.log_message(f"❌ 로그인 체크 실패: JSON 응답 디코딩 실패. 응답 텍스트: {res.text[:100]}")
                self.log_message("UI_ERROR:로그인 실패: 예상치 못한 서버 응답.")
                return {'result': 'fail', 'message': 'JSON decode error'}

            # 5. 예약 페이지 초기 진입 (세션 확정 및 안정화)
            reserve_headers = self.get_base_headers(LOGIN_CHK_URL)
            reserve_headers[
                "Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
            reserve_headers.pop("Content-Type", None)
            reserve_headers.pop("X-Requested-With", None)

            res_reserve = self.session.get(self.RESERVATION_PAGE_URL, headers=reserve_headers, timeout=10,
                                           verify=False)
            res_reserve.raise_for_status()
            # FIX: message_queue 인자 제거
            self.log_message("✅ 예약 페이지 초기 진입 완료.")
            self.member_id = usrid

        except requests.RequestException as e:
            # FIX: message_queue 인자 제거
            self.log_message(f"❌ 네트워크 오류: 로그인 또는 예약 페이지 진입 실패: {e}")
            self.log_message("UI_ERROR:로그인 중 네트워크 오류 발생!")
            return {'result': 'fail', 'message': 'Network Error during login'}

        return {'result': 'success', 'message': 'Login successful'}

    # 세션 유지 URL
    def keep_session_alive(self, target_dt):
        """Periodically hits a page to keep the session active until target_dt (1분에 1회)."""
        self.log_message("✅ 세션 유지 스레드 시작.")
        keep_alive_url = self.RESERVATION_PAGE_URL  # 예약 페이지
        interval_seconds = 60.0  # 1분에 1회

        while not self.stop_event.is_set() and datetime.datetime.now(self.KST) < target_dt:
            try:
                # Use GET request for session keep-alive
                headers = self.get_base_headers(keep_alive_url)
                headers[
                    "Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
                headers.pop("Content-Type", None)
                headers.pop("X-Requested-With", None)

                self.session.get(keep_alive_url, headers=headers, timeout=10, verify=False, proxies=self.proxies)
                self.log_message("💚 [세션 유지] 세션 유지 요청 완료.")
            except Exception as e:
                self.log_message(f"❌ [세션 유지] 통신 오류 발생: {e}")

            # Precise wait loop to check stop_event frequently
            start_wait = time.monotonic()
            while time.monotonic() - start_wait < interval_seconds:
                if self.stop_event.is_set() or datetime.datetime.now(self.KST) >= target_dt:
                    break
                time.sleep(1)  # Check stop event every second

        if self.stop_event.is_set():
            self.log_message("🛑 세션 유지 스레드: 중단 신호 감지. 종료합니다.")
        else:
            self.log_message("✅ 세션 유지 스레드: 예약 정시 도달. 종료합니다.")

    # 서버 시간 확인 URL
    def get_server_time_offset(self):
        """Fetches server time from HTTP Date header and calculates offset from local KST."""
        url = self.RESERVATION_PAGE_URL  # 메인 예약 페이지
        max_retries = 5
        self.log_message("🔄 경주신라CC 서버 시간 확인 시도...")
        for attempt in range(max_retries):
            try:
                headers = self.get_base_headers(self.API_DOMAIN)
                headers[
                    "Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
                headers.pop("Content-Type", None)
                headers.pop("X-Requested-With", None)

                response = self.session.get(url, headers=headers, timeout=5, verify=False)
                response.raise_for_status()
                server_date_str = response.headers.get("Date")
                if server_date_str:
                    server_time_gmt = parsedate_to_datetime(server_date_str)
                    server_time_kst = server_time_gmt.astimezone(KST)
                    local_time_kst = datetime.datetime.now(KST)
                    time_difference = (server_time_kst - local_time_kst).total_seconds()
                    self.log_message(
                        f"✅ 서버 시간 확인 성공: 서버 KST={server_time_kst.strftime('%H:%M:%S.%f')[:-3]}, 로컬 KST={local_time_kst.strftime('%H:%M:%S.%f')[:-3]}, Offset={time_difference:.3f}초")
                    return time_difference
                else:
                    self.log_message(f"⚠️ 서버 Date 헤더 없음, 재시도 ({attempt + 1}/{max_retries})...")
            except requests.RequestException as e:
                self.log_message(f"⚠️ 서버 시간 요청 실패: {e}, 재시도 ({attempt + 1}/{max_retries})...")
            except Exception as e:
                self.log_message(f"❌ 서버 시간 처리 중 오류: {e}")
                return 0
            time.sleep(0.5)

        self.log_message("❌ 서버 시간 확인 최종 실패. 시간 오차 보정 없이 진행합니다 (Offset=0).")
        return 0

    # 세션 활성화 함수 (경주신라CC 'golfCalendar' 호출)
    # 세션 활성화 함수 (경주신라CC 'golfCalendar' 호출)
    # 세션 활성화 함수 (경주신라CC 'golfCalendar' 호출)
    # 세션 활성화 함수 (경주신라CC 'golfCalendar' 호출)
    def prime_calendar(self, date_str):
        """Calls golfCalendar to set the session's active month."""
        self.log_message(f"🔄 세션 활성화를 위해 예약일({date_str}) 기준 달력 정보 로드 시도...")

        url = self.CALENDAR_URL
        headers = self.get_base_headers(self.RESERVATION_PAGE_URL)
        headers["Accept"] = "text/html, */*; q=0.01"

        # 예약 대상 날짜에서 YYYYMM 형식의 월을 추출
        try:
            target_month = datetime.datetime.strptime(date_str, '%Y%m%d').strftime('%Y%m')
        except ValueError:
            self.log_message(f"❌ 유효하지 않은 예약 날짜 형식: {date_str}")
            return False

        # '흐름도.txt' Source 3 Payload 참조 (workMonth, workDate 수정)
        payload = {
            "clickTdId": "",
            "clickTdClass": "",
            "workMonth": target_month,  # <<< 핵심 수정: 예약일의 월 사용
            "workDate": date_str,  # <<< 핵심 수정: 예약일 사용
            "bookgDate": "",
            "bookgTime": "",
            "bookgCourse": "",
            "searchTime": "",
            "selfTYn": "",
            "golfDiv": "N",
            "temp001": "",
            "bookgComment": "",
            "memberCd": "11",
            "temp007": "",
            "certSeq": "",
            "certNoChk": "",
            "agreeYn": "Y"
        }

        try:
            res = self.session.post(url, headers=headers, data=payload, timeout=5.0, verify=False)
            res.raise_for_status()

            if 'text/html' in res.headers.get('content-type', ''):
                self.log_message(f"✅ 캘린더 로드 응답 (HTML) 수신. 세션 활성화 추정.")
                return True
            else:
                self.log_message(f"❌ 캘린더 응답 유형 오류: {res.headers.get('content-type')}")
                return False

        except requests.RequestException as e:
            self.log_message(f"❌ 캘린더 조회 요청 실패: {e}")
            return False
        except Exception as e:
            self.log_message(f"❌ 캘린더 로드 중 예외 오류: {e}")
            return False

    # 'golfTimeList' 호출 (타임아웃 3초, 즉시 재시도 적용)
    def get_all_available_times(self, date):
        """Fetches available tee times (as HTML) for a given date."""
        self.log_message(f"⏳ {date} 모든 코스 예약 가능 시간대 조회 중 (HTML 요청)...")

        url = self.TIME_LIST_URL
        headers = self.get_base_headers(self.RESERVATION_PAGE_URL)
        headers["Accept"] = "text/html, */*; q=0.01"

        # 예약 대상 날짜에서 YYYYMM 형식의 월을 추출
        try:
            target_month = datetime.datetime.strptime(date, '%Y%m%d').strftime('%Y%m')
        except ValueError:
            self.log_message(f"❌ 유효하지 않은 예약 날짜 형식: {date}")
            return None

        # '흐름도.txt' Source 4 Payload 참조 (workMonth 수정)
        payload = {
            "clickTdId": f"B{date}",
            "clickTdClass": "",
            "workMonth": target_month,
            "workDate": date,
            "bookgDate": "",
            "bookgTime": "",
            "bookgCourse": "ALL",
            "searchTime": "",
            "selfTYn": "",
            "golfDiv": "N",
            "temp001": "",
            "bookgComment": "",
            "memberCd": "11",
            "temp007": "",
            "certSeq": "",
            "certNoChk": "",
            "agreeYn": "Y"
        }

        # --- [수정된 3회 재시도 루프: 3.0초 타임아웃, 즉시 재시도] ---
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                # 📌 Timeout 3.0초 설정
                res = self.session.post(url, headers=headers, data=payload, timeout=3.0, verify=False)
                res.raise_for_status()

                if 'text/html' in res.headers.get('content-type', ''):
                    self.log_message(f"✅ 'golfTimeList' HTML 응답 수신. (파싱 시작) (시도 {attempt}/{max_retries})")
                    return res.text  # HTML 텍스트 반환
                else:
                    self.log_message(
                        f"❌ 'golfTimeList' 응답 유형 오류: {res.headers.get('content-type')}. 재시도 {attempt}/{max_retries}...")
                    continue  # time.sleep(0.5) 제거 -> 즉시 재시도

            except requests.RequestException as e:
                self.log_message(f"❌ 'golfTimeList' 네트워크 오류: {e}. 재시도 {attempt}/{max_retries}...")
                continue  # time.sleep(0.5) 제거 -> 즉시 재시도
            except Exception as e:
                self.log_message(f"❌ 'golfTimeList' 예외 오류: {e}. 재시도 {attempt}/{max_retries}...")
                continue  # time.sleep(0.5) 제거 -> 즉시 재시도

        # 3회 최종 실패
        self.log_message(f"❌ 'golfTimeList' {max_retries}회 최종 실패.")
        return None
    # HTML 파싱 및 코스 필터링/정렬 로직 수정
    def filter_and_sort_times(self, all_times_html, start_time_str, end_time_str, target_course_names, is_reverse):
        """
        HTML을 파싱하여 시간대와 코스를 필터링하고 정렬합니다.
        """
        start_time_api = format_time_for_api(start_time_str)  # HHMM
        end_time_api = format_time_for_api(end_time_str)  # HHMM

        if not all_times_html:
            self.log_message("❌ 'golfTimeList'로부터 HTML 응답을 받지 못했습니다. 파싱 중단.")
            return []

        parsed_times = []
        try:
            soup = BeautifulSoup(all_times_html, 'html.parser')

            # 1. 예약 가능한 '<button>' 태그를 모두 찾습니다.
            available_buttons = soup.find_all('button', onclick=lambda h: h and 'golfConfirm' in h)

            self.log_message(f"🔍 HTML 파싱: {len(available_buttons)}개의 예약 가능 버튼 발견.")

            for button in available_buttons:
                try:
                    # 2. 'golfConfirm' 파라미터 추출
                    onclick_str = button['onclick']
                    params_str = onclick_str.split('(')[1].split(')')[0]
                    params = [p.strip().strip("'") for p in params_str.split(',')]

                    if len(params) < 12:
                        self.log_message(f"⚠️ 파싱 경고: 'golfConfirm' 파라미터 개수 부족 ({len(params)}개). 건너뜀.")
                        continue

                    # 3. 핵심 정보 추출
                    bk_time_api = params[1]  # '0600'
                    bk_cos_code = params[2]  # '1'
                    course_nm = params[3]  # '천마OUT'
                    temp007_token = params[11]  # 'DC5F9E003066B106' (12번째 파라미터)

                    # 4. 시간 필터링 (UI 기준)
                    if start_time_api <= bk_time_api <= end_time_api:
                        # 예약에 필요한 모든 정보를 튜플로 전달
                        # (bk_time, bk_cos, course_nm, token)
                        parsed_times.append(
                            (bk_time_api, bk_cos_code, course_nm, temp007_token)
                        )
                except Exception as e:
                    self.log_message(f"⚠️ HTML 버튼 1개 파싱 중 오류: {e}")

        except Exception as e:
            self.log_message(f"❌ HTML 파싱 중 치명적 오류: {e}")
            self.log_message("UI_ERROR:HTML 파싱 라이브러리(BeautifulSoup) 오류 발생.")
            return []  # 파싱 실패 시 빈 리스트 반환

        if not parsed_times and all_times_html:
            self.log_message("ℹ️ HTML 파싱은 성공했으나, 설정된 시간 내 예약 가능한 버튼이 없습니다.")

        # --- [NEW LOGIC START] ---

        # 5. 코스 필터링: target_course_names (ALL, 천마, 화랑)에 따라 필터링
        final_filtered_times = []

        # 5-1. 사용자 선택에 따른 허용 코스 목록 정의
        allowed_courses = []
        if target_course_names == "천마":
            allowed_courses = ["천마OUT", "천마IN"]
        elif target_course_names == "화랑":
            allowed_courses = ["화랑OUT", "화랑IN"]
        elif target_course_names == "ALL":
            allowed_courses = ["천마OUT", "천마IN", "화랑OUT", "화랑IN"]

        # 5-2. 코스 필터링 적용
        for time_info in parsed_times:
            # time_info[2] is course_nm (e.g., '천마OUT')
            if time_info[2] in allowed_courses:
                final_filtered_times.append(time_info)

        # 6. 정렬
        # 튜플 구조 (bk_time, bk_cos, course_nm, token) 기준 정렬
        # 시간(x[0])이 1차 정렬 기준, 코스 코드(x[1])가 2차 정렬 기준 (안정성)
        final_filtered_times.sort(key=lambda x: (x[0], x[1]), reverse=is_reverse)

        # 7. 상위 5개 로그 출력
        formatted_times = [f"{format_time_for_display(t[0])} ({t[2]})" for t in
                           final_filtered_times]  # t[2] = course_nm

        self.log_message(f"🔍 필터링/정렬 완료 (순서: {'역순' if is_reverse else '순차'}) - {len(final_filtered_times)}개 발견")
        if formatted_times:
            self.log_message("📜 **[최종 예약 우선순위 5개]**")
            for i, time_str in enumerate(formatted_times[:5]):
                self.log_message(f"   {i + 1}순위: {time_str}")

        return final_filtered_times

    # 예약 시도 로직 (2단계 최종본 반영)
    def try_reservation(self, date, course_code, time_api, temp007_token, cookies):
        """
        'golfNoChk' (1단계) 및 'golfSubmit' (2단계)를 순차적으로 시도합니다.
        (수정됨: 예약 성공 기준 명확화, 5초 대기 제거, 간결한 로그, (bool, str) 반환)
        """
        course_name = self.course_detail_mapping.get(course_code, f"코스({course_code})")
        time_display = format_time_for_display(time_api)
        today_month = datetime.datetime.now(self.KST).strftime('%Y%m')

        # ------------------------------------------------------------------
        # ⛔ 1단계: golfNoChk 호출 (인증번호 및 certSeq 받기)
        # ------------------------------------------------------------------

        url_step1 = self.BOOK_CHECK_URL
        headers_step1 = self.get_base_headers(self.RESERVATION_PAGE_URL)
        headers_step1["Accept"] = "application/json, text/javascript, */*; q=0.01"

        # '흐름도.txt' Source 5 Payload 참조
        payload_step1 = {
            "clickTdId": f"B{date}",
            "clickTdClass": "",
            "workMonth": today_month,
            "workDate": date,
            "bookgDate": date,
            "bookgTime": time_api,
            "bookgCourse": course_code,
            "searchTime": "",
            "selfTYn": "N",
            "golfDiv": "N",
            "temp001": "",
            "bookgComment": "",
            "memberCd": "11",  # 세션 캘린더/리스트 값 사용
            "temp007": temp007_token,  # 파싱된 토큰 사용
            "certSeq": "",
            "certNoChk": "",
            "agreeYn": "Y"
        }

        cert_seq = None
        auth_number = None

        try:
            res_step1 = self.session.post(url_step1, headers=headers_step1, cookies=cookies, data=payload_step1,
                                          timeout=10, verify=False)
            res_step1.raise_for_status()

            # Source 5 응답은 JSON
            data_step1 = res_step1.json()

            # 🔔 로그 간소화: 성공 여부만 출력
            self.log_message(f"✅ 1단계('golfNoChk') 응답 수신: success='{data_step1.get('success')}'")

            # 'certSeq'와 '인증번호' 추출
            cert_seq = data_step1.get('certSeq')
            auth_number = data_step1.get('certNo')

            if not auth_number:
                auth_number = data_step1.get('certNoChk')
            if not auth_number:
                auth_number = data_step1.get('golfTimeDiv2CertNo')

            # 중첩된 구조(resultData) 확인
            if not cert_seq and 'resultData' in data_step1 and isinstance(data_step1['resultData'], dict):
                cert_seq = data_step1['resultData'].get('certSeq')
            if not auth_number and 'resultData' in data_step1 and isinstance(data_step1['resultData'], dict):
                auth_number = data_step1['resultData'].get('certNo')
                if not auth_number:
                    auth_number = data_step1['resultData'].get('certNoChk')

            if not auth_number or not cert_seq:
                fail_msg = data_step1.get('message', '1단계 응답에서 certSeq 또는 인증번호(certNo)를 찾을 수 없음')
                self.log_message(f"❌ 1단계 실패: {fail_msg}")
                # [수정] 실패 시 메시지 반환
                return False, f"1단계 인증 실패: {fail_msg}"

            self.log_message(f"✅ 1단계 성공: certSeq='{cert_seq}', auth_number='{auth_number}' 확보.")

        except requests.RequestException as e:
            self.log_message(f"❌ 1단계('golfNoChk') 네트워크 오류: {e}")
            return False, f"1단계 네트워크 오류: {e}"
        except json.JSONDecodeError:
            self.log_message(f"❌ 1단계('golfNoChk') JSON 파싱 오류: {res_step1.text[:200]}")
            return False, "1단계 JSON 파싱 오류"

        # ------------------------------------------------------------------
        # ⛔ 2단계: golfSubmit 호출 (최종 예약)
        # ------------------------------------------------------------------

        url_step2 = self.BOOK_SUBMIT_URL
        headers_step2 = self.get_base_headers(self.RESERVATION_PAGE_URL)
        headers_step2["Accept"] = "application/json, text/javascript, */*; q=0.01"

        # '흐름도.txt' Source 7 Payload 참조
        payload_step2 = {
            "clickTdId": f"B{date}",
            "clickTdClass": "",
            "workMonth": today_month,
            "workDate": date,
            "bookgDate": date,
            "bookgTime": time_api,
            "bookgCourse": course_code,
            "searchTime": "",
            "selfTYn": "N",
            "golfDiv": "N",
            "temp001": "",
            "bookgComment": "",
            "memberCd": "11",
            "temp007": temp007_token,
            "certSeq": cert_seq,  # 1단계에서 받은 값
            "certNoChk": auth_number,  # 1단계에서 받은 값
            "agreeYn": "Y"
        }

        try:
            res_step2 = self.session.post(url_step2, headers=headers_step2, cookies=cookies, data=payload_step2,
                                          timeout=10, verify=False)
            res_step2.raise_for_status()

            # Source 6 응답은 JSON
            data_step2 = res_step2.json()

            # 🔔 로그 간소화: 성공/실패만 출력 (상세 메시지는 반환값으로 전달)

            # ✅ 성공/실패 여부 판단 (핵심: 'success' == 'S' 확인)
            is_success_api = data_step2.get('success') == 'S'
            return_msg = data_step2.get('returnMsg', '')
            resno = data_step2.get('resInfo', {}).get('resno', 'N/A')

            if is_success_api:
                # [수정] 불필요한 5초 대기 코드 제거
                self.log_message(f"🎉 2단계('golfSubmit') 최종 성공! (예약번호: {resno})")
                # [수정] 성공 메시지와 True 반환
                return True, return_msg
            else:
                # 실패 시 메시지를 50자로 제한하여 출력
                limited_msg = return_msg.replace('\r', ' ').replace('\n', ' ')
                self.log_message(f"❌ 2단계('golfSubmit') 실패: {limited_msg[:50]}...")
                # [수정] 실패 메시지와 False 반환
                return False, return_msg

        except requests.RequestException as e:
            self.log_message(f"❌ 2단계('golfSubmit') 네트워크 오류: {e}")
            return False, f"2단계 네트워크 오류: {e}"
        except json.JSONDecodeError:
            self.log_message(f"❌ 2단계('golfSubmit') JSON 파싱 오류: {res_step2.text[:200]}")
            return False, "2단계 JSON 파싱 오류"
        except Exception as e:
            self.log_message(f"❌ 2단계('golfSubmit') 중 예외 오류: {e}")
            return False, f"2단계 예외 오류: {e}"

    def run_api_booking(self, inputs, sorted_available_times):
        """Attempts reservation on sorted times, up to top 5, with 3-retry logic."""
        if not sorted_available_times:
            self.log_message("ℹ️ 설정된 조건에 맞는 예약 가능 시간대가 없습니다. API 예약 중단.")
            return False

        target_date = inputs['target_date']
        test_mode = inputs.get('test_mode', True)
        cookies = self.session.cookies

        # Test mode logic (변경 없음)
        if test_mode:
            # 튜플 구조: (bk_time, bk_cos, course_nm, token)
            if not sorted_available_times:
                self.log_message("✅ 테스트 모드: 예약 가능한 시간이 없습니다.")
                return True
            first_time_info = sorted_available_times[0]
            formatted_time = f"{format_time_for_display(first_time_info[0])} ({first_time_info[2]})"  # bk_time, course_nm

            self.log_message(f"✅ 테스트 모드: 1순위 예약 가능 시간 확인: {formatted_time} (실제 예약 시도 안함)")
            return True  # Indicate test mode completion

        self.log_message(f"🔎 정렬된 시간 순서대로 (상위 {min(5, len(sorted_available_times))}개) 예약 시도...")

        # Try booking the top 5
        for i, time_info in enumerate(sorted_available_times[:5]):
            if self.stop_event.is_set():
                self.log_message("🛑 예약 시도 중 중단됨.")
                break

            # 튜플 구조: (bk_time, bk_cos, course_nm, token)
            bk_time_api = time_info[0]
            bk_cos_code = time_info[1]
            course_nm = time_info[2]
            temp007_token = time_info[3]
            time_display = format_time_for_display(bk_time_api)

            # --- 3회 재시도 루프 시작 ---
            for attempt in range(1, 4):
                if self.stop_event.is_set(): break

                self.log_message(f"➡️ [시도 {i + 1}/5 - 재시도 {attempt}/3] 예약 시도: {course_nm} {time_display}")

                is_success, return_msg = self.try_reservation(
                    target_date, bk_cos_code, bk_time_api, temp007_token, cookies
                )

                if is_success:
                    self.log_message(f"🎉🎉🎉 최종 예약 성공!!! [{i + 1}순위] {course_nm} {time_display}")
                    return True  # 전체 프로세스 성공

                # ⚠️ 중복 예약 실패 메시지 확인 (핵심 수정)
                # 이 메시지는 일반적으로 이 루프 이전의 다른 시간대 예약이 성공했음을 의미합니다.
                if '동일한 일자에 예약된 타임이' in return_msg:
                    self.log_message("✅ [중복 감지] 다른 시간대가 이미 예약되었음이 확인되어, 프로세스를 성공적으로 종료합니다.")
                    self.log_message("UI_SUCCESS:✅ 다른 시간대 예약 성공! 중복 예약 시도를 종료합니다.")  # 최종 성공 로그
                    return True  # 전체 프로세스 성공

                # ❌ 현재 시도 실패 시
                if attempt < 3:
                    self.log_message(f"❌ {course_nm} {time_display} 예약 요청 실패. 3초 대기 후 재시도...")
                    time.sleep(3)

            # 3회 시도 모두 실패 시 다음 시간대로 이동
            if not is_success:
                self.log_message(f"❌ {course_nm} {time_display} 예약 시도 3회 모두 최종 실패. 다음 시간대로 이동.")

        # Outer loop (top 5 times) finished without success
        if not self.stop_event.is_set():
            self.log_message(f"❌ 상위 {min(5, len(sorted_available_times))}개 시간대 예약 시도 최종 실패.")

        return False


# ============================================================
# Main Threading Logic - start_pre_process (경주신라CC 맞춤)
# ============================================================
def start_pre_process(message_queue, stop_event, inputs):
    """Main background thread function orchestrating the booking process."""
    global KST

    # 📌 안전 마진 설정 (이전과 동일하게 유지)
    SAFETY_MARGIN_SECONDS = 0.200  # 0.2초 안전 마진 설정

    log_message("[INFO] ⚙️ 예약 시작 조건 확인 완료.", message_queue)

    try:
        core = APIBookingCore(log_message, message_queue, stop_event)

        # 1. Login
        log_message("🔒 로그인 시도...", message_queue)
        login_result = core.requests_login(inputs['id'], inputs['password'])
        if login_result['result'] != 'success':
            log_message(f"❌ 로그인 실패: {login_result['message']}", message_queue)
            return
        log_message("✅ 로그인 성공.", message_queue)

        if stop_event.is_set(): return

        # 2. Server Time Check & Target Time Calculation (Initial Offset)
        log_message("🔄 경주신라CC 서버 시간 확인 시도...", message_queue)
        time_offset = core.get_server_time_offset()

        # 목표 시간을 서버 시간 오프셋을 반영하여 계산 (초기값)
        target_dt_naive = datetime.datetime.strptime(f"{inputs['run_date']}{inputs['run_time']}", '%Y%m%d%H:%M:%S')
        target_dt_kst = KST.localize(target_dt_naive)

        # target_local_time_kst는 로직이 진행됨에 따라 계속 업데이트됩니다. (최초 보정)
        # 📌 0.2초 안전 마진 추가
        target_local_time_kst = target_dt_kst - datetime.timedelta(seconds=time_offset) + datetime.timedelta(
            seconds=SAFETY_MARGIN_SECONDS)

        log_message(
            f"✅ [초기 목표 시간] Local KST 기준: {target_local_time_kst.strftime('%H:%M:%S.%f')[:-3]} (Offset: {time_offset:.3f}초 반영, 안전 마진: {SAFETY_MARGIN_SECONDS:.3f}초 포함)",
            message_queue)

        # 3. FIX: Calendar Context Setting/Navigation
        log_message(f"🔎 **[선행 작업]** 달력 정보 로드 (세션 활성화)...", message_queue)
        calendar_primed = core.prime_calendar(inputs['target_date'])

        if stop_event.is_set(): return

        if not calendar_primed:
            log_message("❌ 달력 정보 로드에 실패했습니다. 예약 프로세스를 중단합니다.", message_queue)
            log_message("UI_ERROR:달력(세션) 초기화 실패로 예약 프로세스 중단.", message_queue)
            return
        log_message("✅ 달력 컨텍스트 설정 완료. 세션 활성화.", message_queue)

        # 4. Session Keep-Alive Thread Start
        # 세션 유지 스레드는 초기 목표 시간을 기준으로 5초 전에 종료됨
        keep_alive_dt = target_local_time_kst - datetime.timedelta(seconds=5)
        keep_alive_thread = threading.Thread(
            target=core.keep_session_alive,
            args=(keep_alive_dt,),
            daemon=True
        )
        keep_alive_thread.start()
        log_message("✅ 세션 유지 스레드 백그라운드 시작.", message_queue)

        # 5. 예약 지연 시간 설정 (Test Mode)
        booking_delay = max(0.0, float(inputs['delay']))
        if inputs['test_mode'] or booking_delay > 0.001:
            log_message(f"⏳ 예약 시도 지연 시간 설정: {booking_delay:.3f}초 (골든 타임 직후 대기)", message_queue)

        if stop_event.is_set(): return

        # 6. Wait until the Re-Synchronization Point (Target Time - 30 seconds)
        now_kst = datetime.datetime.now(KST)

        # 재동기화 시점: 최종 예약 목표 시간 (Target Server Time)의 30초 전 시점
        # target_dt_kst는 서버 10:00:00을 가리킵니다. 여기에 오프셋이 반영되지 않은 순수 목표 시간
        target_dt_naive_server = target_dt_kst.replace(tzinfo=None)  # 순수 목표 시간
        target_dt_server = KST.localize(target_dt_naive_server)

        # 서버 시간 기준 30초 전 시점을 로컬 시간으로 변환
        re_sync_dt_kst = target_local_time_kst - datetime.timedelta(seconds=30) + datetime.timedelta(
            seconds=SAFETY_MARGIN_SECONDS)

        if now_kst < re_sync_dt_kst:
            # 6-1. 30초 전 시점까지 대기 (카운트다운 없음)
            log_message(
                f"⏳ 최종 예약 30초 전 시점({re_sync_dt_kst.strftime('%H:%M:%S.%f')[:-3]})까지 대기합니다.",
                message_queue)

            # log_countdown=False로 설정하여 30초 전 대기 중에는 로그를 남기지 않음
            wait_until(re_sync_dt_kst, stop_event, message_queue, "재동기화 시점 도달", log_countdown=False)

            if stop_event.is_set(): return

            # 6-2. Perform Re-Synchronization (Exactly 30 seconds before)
            log_message("⏳ 최종 예약 30초 전: 서버 시간 오차 재측정 및 보정 (부하 최소화 시점)", message_queue)
            final_time_offset = core.get_server_time_offset()

            # ❗❗ 최종 목표 시간(target_dt_kst)에 새로 측정된 오프셋과 안전 마진을 반영하여 덮어씁니다. ❗❗
            target_local_time_kst = target_dt_kst - datetime.timedelta(seconds=final_time_offset) + datetime.timedelta(
                seconds=SAFETY_MARGIN_SECONDS)

            log_message(
                f"✅ 최종 목표 시간 재확정 (Local KST): {target_local_time_kst.strftime('%H:%M:%S.%f')[:-3]} (최종 Offset: {final_time_offset:.3f}초 반영, 안전 마진: {SAFETY_MARGIN_SECONDS:.3f}초 포함)",
                message_queue)

        else:
            # 30초 전 시점보다 현재 시간이 늦은 경우 (즉시 실행)
            log_message("⚠️ [시간 경과] 이미 최종 예약 30초 전 시점을 지났습니다. 초기 오프셋으로 즉시 실행합니다.", message_queue)

        if stop_event.is_set(): return

        # 7. Wait until the Final Target Time (with Countdown)
        # 이제 최종 보정된 target_local_time_kst를 기준으로 30초 카운트다운을 포함하여 대기합니다.
        wait_until(target_local_time_kst, stop_event, message_queue, "최종 예약 시도", log_countdown=True)

        if stop_event.is_set(): return

        # 8. Fetch, Filter, Sort Tee Times
        log_message("🔎 🚀 **[골든 타임]** 티 타임 조회 시작 (HTML 요청)...", message_queue)
        all_times_html = core.get_all_available_times(inputs['target_date'])

        if stop_event.is_set(): return

        if not all_times_html:
            log_message(f"❌ 'golfTimeList'로부터 HTML 응답을 받지 못했습니다. 파싱 중단.", message_queue)
            log_message(f"❌ 예약 프로세스 실패.", message_queue)
            return

        log_message(
            f"🔎 필터링 조건: {inputs['start_time']}~{inputs['end_time']}, 코스: {inputs['course_type']}, 순서: {inputs['order']}",
            message_queue)

        sorted_times = core.filter_and_sort_times(
            all_times_html,
            inputs['start_time'],
            inputs['end_time'],
            inputs['target_course'],
            inputs['reverse_order']
        )

        if not sorted_times and not stop_event.is_set():
            log_message("ℹ️ 설정된 조건에 맞는 예약 가능 시간대가 없습니다. API 예약 중단.", message_queue)
            log_message(f"❌ 예약 프로세스 실패.", message_queue)
            return

        # 📌 [수정된 위치] 9. Apply Booking Delay (예약 지연) - 정렬 완료 후, 예약 시도 직전
        # 이 지연은 '티타임 조회 및 정렬 후, 실제 예약 시도 전에' 적용됩니다.
        try:
            if booking_delay > 0.001:
                log_message(f"⏳ 설정된 예약 지연 ({booking_delay:.3f}초) 적용...", message_queue)
                time.sleep(booking_delay)
        except Exception:
            pass

        if stop_event.is_set(): return

        # 10. Start Booking Sequence (최종 예약 시도)
        if not inputs['test_mode']:
            log_message(f"[API EXEC] 🔥 **[예약 시퀀스]** 총 {len(sorted_times)}개 타임 중 상위 5개 예약 시도...", message_queue)
            success = core.run_api_booking(inputs, sorted_times)
            if not success:
                log_message(f"❌ 예약 프로세스 실패.", message_queue)
                log_message("UI_ERROR:❌ 예약에 실패했습니다. 로그를 확인하세요.", message_queue)
            else:
                log_message("UI_SUCCESS:🎉 예약 프로세스 최종 성공! 로그를 확인하세요.", message_queue)
        else:
            log_message("🚧 **[테스트 모드]** 예약 시퀀스를 건너뜁니다. 예약 가능한 시간만 확인했습니다.", message_queue)

    except Exception as e:
        error_type = type(e).__name__
        error_message = str(e)
        log_message(f"❌ [최종 오류] 예약 스레드에서 예외 발생: {error_type} - {error_message}", message_queue)
        log_message(f"❌ Traceback: {traceback.format_exc()}", message_queue)
        log_message("UI_ERROR:치명적인 오류 발생! 로그를 확인하세요.", message_queue)

    finally:
        # 11. Thread cleanup
        stop_event.set()
        try:
            if 'keep_alive_thread' in locals() and keep_alive_thread.is_alive():
                log_message("⏳ 세션 유지 스레드 종료 대기...", message_queue)
                keep_alive_thread.join(timeout=5)
        except Exception:
            pass
        log_message("✅ 백그라운드 스레드 종료.", message_queue)
# ============================================================
# Streamlit UI
# ============================================================

# Initialize Session State Variables
# Initialize Session State Variables
if 'log_messages' not in st.session_state:
    st.session_state.log_messages = ["프로그램 실행 준비 완료."]
if 'is_running' not in st.session_state:
    st.session_state.is_running = False
if 'stop_event' not in st.session_state:
    st.session_state.stop_event = threading.Event()
if 'booking_thread' not in st.session_state:
    st.session_state.booking_thread = None
if 'message_queue' not in st.session_state:
    st.session_state.message_queue = queue.Queue()
if 'inputs' not in st.session_state:
    st.session_state.inputs = {}
if 'run_id' not in st.session_state:
    st.session_state.run_id = None
if 'log_container_placeholder' not in st.session_state:
    st.session_state.log_container_placeholder = None
if '_button_clicked_status_change' not in st.session_state:
    st.session_state._button_clicked_status_change = False

# --- [수정된 부분] Default Input Values ---
# 초기값을 먼저 세션 상태에 확정적으로 설정합니다.
if 'id_input' not in st.session_state: st.session_state.id_input = ""
if 'pw_input' not in st.session_state: st.session_state.pw_input = ""
if 'date_input' not in st.session_state:
    today = datetime.datetime.now(KST)
    next_month_first_day = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    next_month_last_day = (next_month_first_day.replace(day=28) + datetime.timedelta(days=4)).replace(
        day=1) - datetime.timedelta(days=1)
    default_booking_day = min(today.day, next_month_last_day.day)
    st.session_state.date_input = next_month_first_day.replace(day=default_booking_day).date()
if 'run_date_input' not in st.session_state:
    st.session_state.run_date_input = get_default_date(0).strftime('%Y%m%d')  # Today
if 'run_time_input' not in st.session_state:
    st.session_state.run_time_input = "10:00:00"  # Default 10:00:00 KST
if 'res_start_input' not in st.session_state:
    st.session_state.res_start_input = "07:00"  # Default 07:00
if 'res_end_input' not in st.session_state:
    st.session_state.res_end_input = "09:00"  # Default 09:00
if 'course_input' not in st.session_state:
    st.session_state.course_input = "ALL"  # Default ALL courses
if 'order_input' not in st.session_state:
    st.session_state.order_input = "역순(▼)"  # Default to Reverse order
if 'delay_input' not in st.session_state:
    st.session_state.delay_input = "1.0"  # Default delay
if 'test_mode_checkbox' not in st.session_state:
    st.session_state.test_mode_checkbox = True  # Default to Test Mode ON
# [새로 추가] ID 유효성 상태를 추적하는 변수
if 'is_id_valid' not in st.session_state:
    st.session_state.is_id_valid = False

# --- Callback Functions ---
def stop_booking():
    """Callback for the '중단/취소' button."""
    if not st.session_state.is_running: return
    log_message("🛑 사용자가 '취소' 버튼을 클릭했습니다. 프로세스 종료 중...", st.session_state.message_queue)
    st.session_state.stop_event.set()
    st.session_state.is_running = False
    st.session_state.run_id = None
    st.session_state.is_id_valid = False  # [이 줄을 추가하세요]
    st.session_state._button_clicked_status_change = True  # Signal state change


# [새로 추가] ID 입력 필드 변경 시 즉시 유효성 검사
def validate_id_on_change():
    """
    ID 입력창에서 포커스가 벗어날 때(on_change) 호출되어
    즉시 ID 유효성을 검사하고, st.session_state.is_id_valid 상태를 설정합니다.
    """
    entered_id = st.session_state.id_input.strip()

    # 1. ID가 비어있으면, 즉시 '유효하지 않음'으로 설정하고 종료.
    if not entered_id:
        st.session_state.is_id_valid = False
        return

    try:
        # 2. login_ids.txt 파일 읽기
        with open("login_ids.txt", 'r', encoding='utf-8') as f:
            allowed_ids = {line.strip() for line in f if line.strip()}

        # 3. ID 유효성 검사
        if entered_id in allowed_ids:
            # [핵심] ID가 유효하면 True로 설정
            st.session_state.is_id_valid = True
            st.toast(f"✅ {entered_id}님, 환영합니다!", icon="👋")
        else:
            # [핵심] ID가 유효하지 않으면 False로 설정
            st.session_state.is_id_valid = False
            st.toast("사용할수 없는 사용자 입니다..!!", icon="❌")

    except FileNotFoundError:
        st.session_state.is_id_valid = False  # 파일 없으면 무조건 False
        st.toast("ID Txt파일(login_ids.txt)이 없습니다.", icon="❌")
    except Exception as e:
        st.session_state.is_id_valid = False  # 오류 나도 무조건 False
        st.toast(f"ID 파일 읽기 오류: {e}", icon="❌")

def run_booking():
    """Starts the booking process thread."""

    # --- [수정된 ID 유효성 검사 로직] ---

    # 1. ID 입력 확인 (양쪽 공백 제거)
    entered_id = st.session_state.id_input.strip()
    if not entered_id:
        st.session_state.message_queue.put("UI_ERROR:ID를 입력해주세요.")
        st.session_state._button_clicked_status_change = True
        return

    # 2. login_ids.txt 파일 읽기
    try:
        # login_ids.txt 파일에서 허용된 ID 목록을 읽어옵니다.
        with open("login_ids.txt", 'r', encoding='utf-8') as f:
            allowed_ids = {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        st.session_state.message_queue.put("UI_ERROR:ID Txt파일(login_ids.txt)이 없습니다. 관리자에게 문의하세요.")
        st.session_state._button_clicked_status_change = True
        return
    except Exception as e:
        st.session_state.message_queue.put(f"UI_ERROR:ID 파일 읽기 오류: {e}")
        st.session_state._button_clicked_status_change = True
        return

    # 3. ID 유효성 검사
    if entered_id not in allowed_ids:
        # 요청하신 에러 메시지
        st.session_state.message_queue.put("UI_ERROR:사용할수 없는 사용자 입니다..!!")
        st.session_state._button_clicked_status_change = True
        return

    # --- [수정 완료] ---

    # 4. (기존 로직) 비밀번호 확인
    if not st.session_state.pw_input:
        st.session_state.message_queue.put("UI_ERROR:비밀번호를 입력해주세요.")
        st.session_state._button_clicked_status_change = True
        return

    if st.session_state.is_running:
        return

    # Clear previous state
    st.session_state.message_queue = queue.Queue()
    st.session_state.stop_event = threading.Event()
    st.session_state.log_messages = []

    # Create a unique ID for the run (for RERUN check)
    st.session_state.run_id = datetime.datetime.now(KST).isoformat()
    st.session_state.is_running = True
    st.session_state._button_clicked_status_change = True

    # Inputs Dictionary (YYYYMMDD 형식으로 변환)
    target_date_str = st.session_state.date_input.strftime('%Y%m%d')

    # *************************************************************
    # FIX: 'password' 키에 'pw_input' 세션 상태 값을 할당하도록 수정
    # *************************************************************
    st.session_state.inputs = {
        'id': st.session_state.id_input,
        'password': st.session_state.pw_input,  # <<< 수정됨: 'pw_input' 사용
        'target_date': target_date_str,  # YYYYMMDD
        'run_date': st.session_state.run_date_input,  # YYYYMMDD
        'run_time': st.session_state.run_time_input,  # HH:MM:SS
        'start_time': st.session_state.res_start_input,  # HH:MM
        'end_time': st.session_state.res_end_input,  # HH:MM
        'course_type': st.session_state.course_input,  # Course Name or "전체"
        'order': st.session_state.order_input,  # "순차(▲)" or "역순(▼)"
        'delay': st.session_state.delay_input,  # Delay string (float convertible)
        'test_mode': st.session_state.test_mode_checkbox,  # Boolean
        'target_course': st.session_state.course_input,  # 스레드 내부에서 'All'로 강제됨
        'reverse_order': st.session_state.order_input == "역순(▼)"  # Boolean
    }

    # 4. Start the Background Thread
    st.session_state.booking_thread = threading.Thread(
        target=start_pre_process,
        args=(st.session_state.message_queue, st.session_state.stop_event, st.session_state.inputs),
        daemon=True
    )
    st.session_state.booking_thread.start()


# --- Real-time Update Function ---
def check_queue_and_rerun():
    """Checks the message queue and triggers rerun if needed."""
    if st.session_state.run_id is None: return

    new_message_received = False
    is_running_before_check = st.session_state.is_running
    ui_error_occurred = False  # Flag to check if UI error stopped the process

    # Process all messages in the queue
    while not st.session_state.message_queue.empty():
        try:
            message = st.session_state.message_queue.get_nowait()
        except queue.Empty:
            break

        if message.startswith("UI_ERROR:"):
            error_text = message.replace("UI_ERROR:", "[UI ALERT] ❌ ")
            st.session_state.log_messages.append(error_text)
            st.session_state.is_running = False  # Stop on error
            st.session_state.stop_event.set()  # Signal thread to stop
            st.session_state.run_id = None
            new_message_received = True
            ui_error_occurred = True  # Mark that an error stopped the process
            break  # Stop processing messages on UI error
        elif message.startswith("UI_LOG:"):
            log_text = message.replace("UI_LOG:", "")
            st.session_state.log_messages.append(log_text)
            new_message_received = True

    # Check if the thread has finished on its own (only if no UI error occurred)
    if is_running_before_check and not ui_error_occurred:
        if st.session_state.booking_thread and not st.session_state.booking_thread.is_alive():
            # Thread finished without explicit UI error or stop button
            st.session_state.is_running = False
            st.session_state.run_id = None
            new_message_received = True  # Ensure rerun to update button state

    # Rerun if new messages arrived OR if the process finished/stopped
    if new_message_received:
        st.rerun()
        return

    # If still running and no new messages, schedule next check/rerun
    if st.session_state.is_running and st.session_state.run_id is not None:
        time.sleep(0.1)  # Short delay to prevent excessive reruns
        st.rerun()  # Trigger rerun for continuous log checking


# ============================================================
# UI 레이아웃
# ============================================================
# 📌 [추가] 디버깅 및 캐시 문제 해결을 위한 초기화 로직
# 📌 [수정] 예약일 디폴트 값을 당일로 설정
KST = pytz.timezone('Asia/Seoul') # KST 정의가 위에 있는지 확인하세요.
today = datetime.datetime.now(KST).date()
default_date = today # 디폴트 값을 오늘 날짜로 설정

if "is_running" not in st.session_state:
    st.session_state.is_running = False

# date_input 키가 Session State에 없으면 (첫 실행 시) 디폴트 값으로 설정
if "date_input" not in st.session_state:
    st.session_state.date_input = default_date

st.set_page_config(layout="wide", menu_items=None)

# --- CSS Styling ---
st.markdown("""
<style>
    /* Reset margins/padding */
    div[data-testid="stAppViewContainer"] > section,
    div[data-testid="stVerticalBlock"] { margin-top: 0px !important; padding-top: 0px !important; }
    .main > div { padding-top: 0rem !important; }

    /* Title Styling */
    .app-title {
        font-size: 26px !important; 
        font-weight: bold;
        margin-top: 10px !important; 
        margin-bottom: 15px !important; 
        text-align: center; 
    }

    /* Input Width Control */
    div[data-testid="stTextInput"],
    div[data-testid="stDateInput"],
    div[data-testid="stSelectbox"] {
        max-width: 220px !important; 
    }

    /* Section Header Styling */
    .section-header {
        font-size: 16px;
        font-weight: bold;
        margin-top: 10px; 
        margin-bottom: 5px; 
    }

    /* Center Align Containers */
    div[data-testid="stVerticalBlock"] > div:nth-child(1) > div:nth-child(1) > div {
        max-width: 500px; 
        margin: 0 auto !important; 
    }
    div[data-testid="stVerticalBlock"] > div:nth-child(3) {
        max-width: 350px; 
        margin: 0 auto !important; 
    }
     div[data-testid="stVerticalBlock"] > div:nth-child(3) button {
        width: 100%;
    }
     div[data-testid="stVerticalBlock"] > div:nth-child(5) {
        max-width: 600px; 
        margin: 0 auto !important; 
    }

</style>
""", unsafe_allow_html=True)

# Language tag injection for browser translation issue
st.markdown(
    """
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
    </head>
    <body>
    """,
    unsafe_allow_html=True
)

# --- Title ---
st.markdown('<p class="app-title">⛳ 경주신라CC 모바일 예약</p>', unsafe_allow_html=True)

# --- 1. Settings Section ---
with st.container(border=True):
    st.markdown('<p class="section-header">🔑 로그인 및 조건 설정</p>', unsafe_allow_html=True)

    # 1-1. Login Credentials
    col1, col2 = st.columns(2)
    with col1:
        # [수정됨] on_change 콜백 추가
        st.text_input(
            "사용자ID",
            key="id_input",
            disabled=st.session_state.is_running,
            on_change=validate_id_on_change  # <-- 이 줄을 추가하세요
        )
    with col2:
        st.text_input("암호", type="password", key="pw_input", disabled=st.session_state.is_running)

    # 1-2. Booking & Execution Time
    st.markdown("---")  # Separator
    st.markdown('<p class="section-header">🗓️ 예약/가동 시간 설정</p>', unsafe_allow_html=True)

    col3, col4, col5 = st.columns([1, 1, 1])
    with col3:
        st.date_input(
            "예약일",
            key="date_input",
            format="YYYY-MM-DD",
            disabled=st.session_state.is_running,
            # value= 인자는 제거된 상태를 유지해야 경고가 사라집니다.
            min_value=today,  # 최소값은 오늘 날짜
        )
    with col4:
        st.text_input("가동시작일 (YYYYMMDD)", key="run_date_input", help="API 실행 기준 날짜",
                      disabled=st.session_state.is_running)
    with col5:
        st.text_input("가동시작시간 (HH:MM:SS)", key="run_time_input", help="API 실행 기준 시간 (KST)")

    # 1-3. Filters & Priority
    st.markdown("---")  # Separator
    st.markdown('<p class="section-header">⚙️ 티타임 필터 및 우선순위</p>', unsafe_allow_html=True)
    col6, col7, col8 = st.columns([2.5, 2.5, 1.5])
    with col6:
        start_time_options = []
        for h in range(6, 17):  # 16시까지 포함해야 하므로 17까지 range 설정
            start_time_options.append(f"{h:02d}:00")
            # 16:30은 16시까지만 조회하므로 제외
            if h < 16:
                start_time_options.append(f"{h:02d}:30")
        # [최종 수정] index 인수를 완전히 제거합니다.
        # 위젯은 key="res_start_input"에 저장된 세션 상태 값을 사용합니다.
        st.selectbox(
            "시작시간 (HH:MM)",
            options=start_time_options,
            key="res_start_input",
            disabled=st.session_state.is_running
        )

        end_time_options = []
        for h in range(7, 18):  # 16시까지 포함해야 하므로 17까지 range 설정
            end_time_options.append(f"{h:02d}:00")
            if h < 17:
                end_time_options.append(f"{h:02d}:30")

        # [최종 수정] index 인수를 완전히 제거합니다.
        # 위젯은 key="res_end_input"에 저장된 세션 상태 값을 사용합니다.
        st.selectbox(
            "종료시간 (HH:MM)",
            options=end_time_options,
            key="res_end_input",
            disabled=st.session_state.is_running
        )
    with col7:
        # <<< 수정된 부분: 코스 선택 옵션 추가 >>>
        st.selectbox(
            "코스선택",
            ["ALL", "천마", "화랑"],  # <--- 이 부분이 수정되었는지 확인
            key="course_input",
            disabled=st.session_state.is_running,
            help="ALL: 전체 코스, 천마: 천마 OUT/IN, 화랑: 화랑 OUT/IN"
        )
        st.selectbox("예약순서", ["역순(▼)", "순차(▲)"], key="order_input", disabled=st.session_state.is_running)
    with col8:
        st.text_input("예약지연(초)", key="delay_input", help="목표 시간 도달 후 추가 대기 시간(초)", disabled=st.session_state.is_running)
        st.checkbox("테스트 모드", key="test_mode_checkbox", help="실제 예약 실행 안함", disabled=st.session_state.is_running)

# --- 2. Action Buttons ---
st.markdown("---")  # Separator
col_start, col_stop, col_spacer = st.columns([1.5, 1.5, 5])
with col_start:
    st.button(
        "🚀 예약 시작",
        on_click=run_booking,
        disabled=st.session_state.is_running or not st.session_state.is_id_valid,
        type="primary",
        help="ID가 유효해야 버튼이 활성화됩니다."  # [추가] 툴팁
    )
with col_stop:
    st.button("❌ 취소", on_click=stop_booking, disabled=not st.session_state.is_running, type="secondary")

# --- 3. Log Section ---
st.markdown("---")  # Separator
st.markdown('<p class="section-header">📝 실행 로그</p>', unsafe_allow_html=True)

if st.session_state.log_container_placeholder is None:
    st.session_state.log_container_placeholder = st.empty()

with st.session_state.log_container_placeholder.container(height=250):
    for msg in reversed(st.session_state.log_messages[-500:]):
        safe_msg = msg.replace("<", "&lt;").replace(">", "&gt;")
        color = "black"
        if "[UI ALERT]" in msg:
            color = "red"
        elif "🎉" in msg or "✅" in msg and "대기중" not in msg:
            color = "green"
        elif "💚 [세션 유지]" in msg or "📜" in msg:
            color = "#008080"
        st.markdown(f'<p style="font-size: 11px; margin: 0px; color: {color}; font-family: monospace;">{safe_msg}</p>',
                    unsafe_allow_html=True)

# --- Start the real-time update loop ---
check_queue_and_rerun()

# --- Rerun handling after button click ---
if st.session_state.get('_button_clicked_status_change', False):
    st.session_state['_button_clicked_status_change'] = False

    st.rerun()
