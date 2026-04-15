# =============================================
# 네이버 부동산 죽전동 매물 모니터링 설정
# =============================================

# 이메일 설정 (네이버 메일 기준)
import os
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "jjuncco@naver.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "네이버_앱비밀번호")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "jjuncco@naver.com")

# 감시 지역 설정
REGION_SEARCH = {
    "시도": "경기도",
    "시군구": "용인시 수지구",
    "읍면동": "죽전동"
}

# cortarNo 직접 입력 (자동 탐색이 안 될 때 사용)
# None으로 두면 위 REGION_SEARCH로 자동 탐색
# 자동 탐색이 실패하면 아래 주석 해제하고 코드 직접 입력
# CORTAR_NO = "4146511000"
CORTAR_NO = None

# 매물 유형 (원하지 않는 것은 삭제)
HOUSE_TYPES = [
    "APT",    # 아파트
    "OPST",   # 오피스텔
    "VL",     # 빌라/연립
    "OR",     # 원룸
]

# 거래 유형 (둘 다, 또는 하나만)
TRADE_TYPES = [
    "A1",  # 매매
    "B1",  # 전세
    "B2",  # 월세
]

# 스냅샷 저장 경로 (변경 불필요)
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "snapshots")

# 요청 간격 (초) - 너무 빠르면 차단될 수 있음
REQUEST_DELAY = 1.5
