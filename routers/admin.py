# 관리자용 API - 통계 , 회원 , 예약, 주차장 수정

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select, func, or_

from sqlalchemy.orm import Session
from starlette import status

from database.db_connection import get_session
from models.models import ParkingInfo, ParkingDaily, User, Reservation
from schema.request import ParkingUpdateRequest
from auth.jwt import decode_access_token

from datetime import date

router = APIRouter(tags=["관리자"])

bearer = HTTPBearer(auto_error=False)


# 관리자 권한 확인 공동 함수
def get_admin_user_id(
        authorization: HTTPAuthorizationCredentials | None = Depends(bearer)
) -> int:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다",
        )

    # 토큰 해독 & 서명 검증
    token_data = decode_access_token(authorization.credentials)

    # 관리자 권한 확인
    if token_data["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다",
        )

    # 검증 통과 -> 관리자 user_id 반환
    return token_data["user_id"]

def expire_past_reservations(session: Session) -> None:
    session.execute(
        update(Reservation)
        .where(Reservation.status == "active")
        .where(Reservation.reserved_date < date.today())
        .values(status="completed")
    )
    session.commit()
#대시보드 요약 카드 집계
#GET / admin / stats / summary
@router.get("/admin/stats/summary", status_code = status.HTTP_200_OK)
def get_summary_stats_handler(
        session: Session = Depends(get_session),
        admin_id: int = Depends(get_admin_user_id),
):
    #총 회원 수
    total_users = session.scalar(select(func.count(User.id)))

    #총 예약 수
    total_reservations = session.scalar(select(func.count(Reservation.id)))

    #오늘 예약 수
    today_reservations = session.scalar(
        select(func.count(Reservation.id))
        .where(Reservation.reserved_date == func.current_date())
    )

    #활성 예약 수
    active_reservations = session.scalar(
        select(func.count(Reservation.id))
        .where(Reservation.status == "active")
    )

    #scalar 결과가 None일 가능성에 대비해 or 0 반영
    return {
        "total_users": total_users or 0,
        "total_reservations": total_reservations or 0,
        "today_reservations": today_reservations or 0,
        "active_reservations": active_reservations or 0,

    }

# 요일별 평균 회전율(이용률)
# GET / admin / stats/ daily
@router.get("/admin/stats/daily", status_code=status.HTTP_200_OK)
def get_daily_stats_handler(
        session: Session = Depends(get_session),
        admin_id: int = Depends(get_admin_user_id),
):
    # 쿼리 구성
    stmt = (
        select(
            func.dayofweek(ParkingDaily.use_date).label("day_of_week"), # 요일 추출( 1 = 일 ~ 7 = 토 )
            func.round(
                func.avg(ParkingDaily.daily_count / ParkingInfo.capacity * 100), 1
            ).label("avg_occupancy_pct")
        )
        .join(ParkingInfo, ParkingDaily.lot_id == ParkingInfo.id)
        .where(ParkingInfo.capacity>0)
        .where(ParkingDaily.daily_count>0)
        .group_by(func.dayofweek(ParkingDaily.use_date))
        .order_by(func.dayofweek(ParkingDaily.use_date))
    )
    result = session.execute(stmt).all() #쿼리 실행, 결과 행 목록 받기
    day_names = {1: "일", 2: "월", 3: "화", 4: "수", 5: "목", 6: "금", 7: "토"}

    return [
        {
            "day_of_week": r.day_of_week,
            "day_name": day_names.get(r.day_of_week, ""),
            #avg_occupancy_pct가 None일 수 있어(데이터 없음) -> or 0으로 반영
            "avg_occupancy_pct": float(r.avg_occupancy_pct or 0),
        }
        for r in result
    ]

# 회원 목록( 페이지네이션 )
# GET / admin / users
@router.get("/admin/users", status_code=status.HTTP_200_OK)
def get_user_handler(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, description="이메일 또는 이름 검색"),
    session: Session = Depends(get_session),
    admin_id: int = Depends(get_admin_user_id),
):
    stmt = select(User)

    if search:

        like = f"%{search}%"
        stmt = stmt.where(User.email.like(like) | User.name.like(like))


    # 전체 건수 ( 검색 조건 반영)
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))

    # 페이지네이션 적용해 실제 목록 조회
    users = session.execute(
        stmt.order_by(User.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).scalars().all()

    return {
        "total": total or 0,
        "page": page,
        "size": size,
        "items": [{
            "id": u.id,
            "email": u.email,
            "name": u.name,
            "role": u.role,
            "created_at": u.created_at
        }
            for u in users]
    }

# 예약 목록 (페이지네이션)
@router.get("/admin/reservations", status_code=status.HTTP_200_OK)
def get_reservations_handler(
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
        admin_id: int = Depends(get_admin_user_id),
):
    #지난 예약을 먼저 완료 처리 -> 목록의 상태가 최신으로 표시됨
    expire_past_reservations(session)
    # 전체 예약 건수(페이지 수 계산용)
    total = session.scalar(select(func.count(Reservation.id)))

    # 최근 예약순으로 이번 페이지 분량만 조회
    reservations = session.execute(
        select(Reservation)
        .order_by(Reservation.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).scalars().all()

    return {
        "total": total or 0,
        "page": page,
        "size": size,
        "items": [{
            "id": r.id,
            "user_id": r.user_id,
            "lot_id": r.lot_id,
            # r.lot:Reservation -> ParkingInfo 관계로 주차장 이름을 바로 참조
            "lot_name": r.lot.lot_name,
            "reserved_date": str(r.reserved_date),
            "created_at": r.created_at
        }
        for r in reservations
        ]
    }

#월별 평균 회전율
#GET / admin / stats/monthly
@router.get("/admin/stats/monthly", status_code = status.HTTP_200_OK)
def get_monthly_stats_handler(
        session: Session = Depends(get_session),
        admin_id: int = Depends(get_admin_user_id),
):
    stmt = (
        select(
            func.month(ParkingDaily.use_date).label("month"),
            func.round(
                func.avg(ParkingDaily.daily_count / ParkingInfo.capacity * 100),1
            ).label("avg_occupancy_pct"),
        )
        .join(ParkingInfo, ParkingDaily.lot_id == ParkingInfo.id)
        .where(ParkingInfo.capacity > 0)
        .where(ParkingDaily.daily_count > 0)
        .group_by(func.month(ParkingDaily.use_date))
        .order_by(func.month(ParkingDaily.use_date))
    )
    result = session.execute(stmt).all()
    return [
        {"month": r.month, "avg_occupancy_pct": float(r.avg_occupancy_pct or 0)}
    for r in result
    ]

#시간대 별 예약 건수
#GET / admin / stats / hourly
@router.get("/admin/stats/hourly", status_code = status.HTTP_200_OK)
def get_hourly_stats_handler(
        session : Session = Depends(get_session),
        admin_id: int = Depends(get_admin_user_id),
):
    stmt = (
        select(
            func.hour(Reservation.created_at).label("hour"),
            func.count(Reservation.id).label("count"),
        )
        .group_by(func.hour(Reservation.created_at))
        .order_by(func.hour(Reservation.created_at))
    )
    result = session.execute(stmt).all()

    if result:
        #실제  데이터가 있을 때 {시간: 건수} 딕셔너리로 변환
        hour_map = {r.hour: int(r.count) for r in result}
        return [{"hour": h, "avg_count":hour_map.get(h,0)} for h in range(24)]
    else:
        #데이터가 전혀 없을때 : 한간공원 이용 패턴을 흉내낸 샘플 데이터 생성(llm이용)
        sample = [0,0,0,0,0,0,2,5,12,25,42,55,60,52,48,55,70,75,65,45,30,18,8,2]
        return [{"hour": h, "avg_count":sample[h]} for h in range(24)]

