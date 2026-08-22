    
import streamlit as st
import pandas as pd
import numpy as np
import requests
import urllib.parse
import re
from difflib import SequenceMatcher

# -----------------------------
# 기본 설정
# -----------------------------
st.set_page_config(
    page_title="당진 맛집 찾기",
    page_icon="🍲",
    layout="wide"
)

API_KEY = st.secrets["GOOGLE_PLACES_API_KEY"]

# Colab에 업로드된 CSV 자동 탐색
import glob
csv_files = glob.glob("*.csv")

if not csv_files:
    st.error("당진 음식점 CSV 파일을 찾을 수 없습니다.")
    st.stop()

CSV_FILE = csv_files[0]


# -----------------------------
# 행정데이터 로드
# -----------------------------
@st.cache_data
def load_admin_data():

    try:
        df = pd.read_csv(CSV_FILE, encoding="cp949")
    except:
        df = pd.read_csv(CSV_FILE, encoding="utf-8-sig")

    active = df[
        df["영업상태명"]
        .astype(str)
        .str.contains("영업|정상", na=False)
    ].copy()

    active["인허가일자"] = pd.to_datetime(
        active["인허가일자"],
        errors="coerce"
    )

    today = pd.Timestamp.today()

    active["업력"] = (
        (today - active["인허가일자"]).dt.days / 365.25
    ).round(1)

    return active


# -----------------------------
# 상호명 정리
# -----------------------------
def clean_business_name(text):

    if pd.isna(text):
        return ""

    text = str(text).lower().strip()

    for word in ["식당", "본점", "직영점"]:
        text = text.replace(word, "")

    return re.sub(
        r"[^0-9a-z가-힣]",
        "",
        text
    )


# -----------------------------
# 도로명 + 건물번호 추출
# -----------------------------
def extract_road_key(address):

    if pd.isna(address):
        return ""

    address = str(address)

    pattern = r"([가-힣0-9]+(?:로|길))\s*(\d+(?:-\d+)?)"

    match = re.search(pattern, address)

    if match:
        return match.group(1) + match.group(2)

    return ""


# -----------------------------
# 상호명 유사도
# -----------------------------
def calc_name_score(name1, name2):

    a = clean_business_name(name1)
    b = clean_business_name(name2)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    if a in b or b in a:
        return 0.95

    return SequenceMatcher(None, a, b).ratio()


# -----------------------------
# 행정데이터에서 업력 찾기
# -----------------------------
def find_business_age(
    google_name,
    google_address,
    admin_df
):

    google_road = extract_road_key(
        google_address
    )

    best = None
    best_score = 0.0

    for _, row in admin_df.iterrows():

        admin_name = row["사업장명"]

        admin_address = row.get(
            "도로명주소",
            ""
        )

        if (
            pd.isna(admin_address)
            or str(admin_address).strip() == ""
        ):
            admin_address = row.get(
                "지번주소",
                ""
            )

        admin_road = extract_road_key(
            admin_address
        )

        n_score = calc_name_score(
            google_name,
            admin_name
        )

        road_match = (
            google_road != ""
            and admin_road != ""
            and google_road == admin_road
        )

        if road_match and n_score >= 0.55:
            total_score = 0.70 + n_score * 0.30

        elif n_score >= 0.95:
            total_score = n_score * 0.70

        else:
            continue

        if total_score > best_score:

            best_score = total_score

            best = {
                "업력": row["업력"]
            }

    return best


# -----------------------------
# Google Places 검색
# 동일 음식 검색은 7일 캐시
# -----------------------------
@st.cache_data(ttl=60 * 60 * 24 * 7)
def search_google_places(food):

    url = "https://places.googleapis.com/v1/places:searchText"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask":
            "places.id,"
            "places.displayName,"
            "places.formattedAddress,"
            "places.rating,"
            "places.userRatingCount,"
            "places.photos"
    }

    data = {
        "textQuery": f"{food} 충남 당진시",
        "languageCode": "ko",
        "regionCode": "KR",
        "pageSize": 20
    }

    response = requests.post(
        url,
        headers=headers,
        json=data,
        timeout=20
    )

    if not response.ok:
        st.error(f"Google API 오류: {response.status_code}")
        st.code(response.text)
        st.stop()

    places = response.json().get("places", [])

    rows = []

    for place in places:

        photos = place.get("photos", [])

        photo_url = ""

        if photos:

            photo_name = photos[0].get("name", "")

            if photo_name:

                photo_url = (
                    "https://places.googleapis.com/v1/"
                    + photo_name
                    + "/media"
                    + "?maxWidthPx=800"
                    + "&key="
                    + API_KEY
                )

        rows.append({
            "식당명":
                place.get("displayName", {}).get("text", ""),

            "Google 평점":
                place.get("rating", np.nan),

            "리뷰 수":
                place.get("userRatingCount", 0),

            "주소":
                place.get("formattedAddress", ""),

            "Place ID":
                place.get("id", ""),

            "대표사진":
                photo_url
        })

    result = pd.DataFrame(rows)

    if not result.empty:
        result = result.drop_duplicates(
            subset=["Place ID"]
        )

    return result


# -----------------------------
# 업력 기준점수
# 정보 없음 = 중립 50점
# -----------------------------
def calc_age_score(age):

    if pd.isna(age):
        return 50.0

    age = max(
        0,
        min(float(age), 30)
    )

    return age / 30 * 100

# =========================================================
# [27] 상단 메인 이미지
# GitHub 저장소의 hero_dangjin.png 사용
# =========================================================

st.image(
    "hero_dangjin.png",
    use_container_width=True
)

st.markdown(
    """
    <style>
    /* 상단 이미지와 메뉴 영역 간격 */
    [data-testid="stImage"] {
        margin-bottom: 0.2rem;
    }

    /* 전체 페이지 폭과 여백 정리 */
    .block-container {
        max-width: 1400px;
        padding-top: 0.5rem;
        padding-bottom: 3rem;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# =========================================================
# [V1-1] 음식 선택 UX
# 대분류 -> 세부 메뉴 -> 직접 검색
# 버튼 선택 시 직접 입력값 자동 초기화
# =========================================================

# [V1-1] 음식 선택 UX
# ...

food_categories = {
   
}

# -----------------------------
# 선택 상태 초기값
# -----------------------------

if "selected_category" not in st.session_state:
    ...

# -----------------------------
# 선택 상태 초기값
# -----------------------------
if "selected_category" not in st.session_state:
    st.session_state.selected_category = "🍚 한식"

if "selected_food" not in st.session_state:
    st.session_state.selected_food = "백반"

if "custom_food" not in st.session_state:
    st.session_state.custom_food = ""


# -----------------------------
# 1. 대분류 선택
# -----------------------------
st.markdown("### 1. 음식 종류")

category_cols = st.columns(7)

for i, category in enumerate(food_categories.keys()):

    category_selected = (
        st.session_state.selected_category == category
    )

    with category_cols[i]:

        if st.button(
            category,
            key=f"category_{category}",
            type="primary" if category_selected else "secondary",
            use_container_width=True
        ):
            st.session_state.selected_category = category

            # 새 카테고리의 첫 음식 선택
            st.session_state.selected_food = food_categories[category][0]

            # 직접 입력값 초기화
            st.session_state.custom_food = ""

            st.rerun()


# -----------------------------
# 2. 세부 메뉴 선택
# -----------------------------
st.markdown("### 2. 세부 메뉴")

current_category = st.session_state.selected_category
current_foods = food_categories[current_category]

menu_cols = st.columns(min(len(current_foods), 7))

for i, food_name in enumerate(current_foods):

    food_selected = (
        st.session_state.selected_food == food_name
        and st.session_state.custom_food == ""
    )

    with menu_cols[i % len(menu_cols)]:

        if st.button(
            food_name,
            key=f"food_{current_category}_{food_name}",
            type="primary" if food_selected else "secondary",
            use_container_width=True
        ):
            st.session_state.selected_food = food_name

            # 중요:
            # 세부 메뉴 버튼을 누르면
            # 이전 직접 검색어 자동 삭제
            st.session_state.custom_food = ""

            st.rerun()


# -----------------------------
# 3. 직접 음식 검색
# -----------------------------
st.markdown("### 3. 다른 음식 직접 검색")

custom_food = st.text_input(
    "직접 검색",
    placeholder="예: 닭갈비, 샤브샤브, 민물새우탕",
    key="custom_food",
    label_visibility="collapsed"
)

# -----------------------------
# 최종 검색 음식 결정
# -----------------------------
if st.session_state.custom_food.strip():
    food = st.session_state.custom_food.strip()
else:
    food = st.session_state.selected_food


st.caption(f"현재 선택 메뉴: **{food}**")

# -----------------------------
# 가중치 설정
# -----------------------------
st.divider()

st.subheader("내 맛집 평가 기준")

col1, col2, col3 = st.columns(3)

with col1:
    rating_weight = st.slider(
        "⭐ Google 평점",
        20, 50, 45, 5
    )

with col2:
    review_weight = st.slider(
        "💬 리뷰 수",
        20, 50, 30, 5
    )

with col3:
    age_weight = st.slider(
        "🕰 업력",
        20, 50, 25, 5
    )

total_weight = (
    rating_weight
    + review_weight
    + age_weight
)

if total_weight == 100:

    st.success(
        f"평점 {rating_weight}% · "
        f"리뷰 {review_weight}% · "
        f"업력 {age_weight}%"
    )

else:

    st.warning(
        f"현재 합계 {total_weight}%입니다. "
        "합계를 100%로 맞춰주세요."
    )


# -----------------------------
# 검색 실행
# -----------------------------
st.divider()

if st.button(
    "🔎 맛집 찾기",
    type="primary",
    use_container_width=True
):

    if not food:

        st.warning("음식을 입력해주세요.")

    elif total_weight != 100:

        st.warning(
            "가중치 합계를 100%로 맞춰주세요."
        )

    else:

        with st.spinner(
            f"당진의 {food} 맛집을 찾고 있습니다..."
        ):

            google_df = search_google_places(
                food.strip()
            )

            if google_df.empty:

                st.warning(
                    "검색된 식당이 없습니다."
                )

                st.stop()

            admin_df = load_admin_data()

            ages = []

            for _, row in google_df.iterrows():

                age_info = find_business_age(
                    row["식당명"],
                    row["주소"],
                    admin_df
                )

                if age_info:
                    ages.append(
                        age_info["업력"]
                    )
                else:
                    ages.append(
                        np.nan
                    )

            google_df["업력"] = ages


            # -------------------------
            # 평점 기준점수
            # -------------------------
            google_df["평점기준점수"] = (
                pd.to_numeric(
                    google_df["Google 평점"],
                    errors="coerce"
                )
                / 5
                * 100
            )


            # -------------------------
            # 리뷰 기준점수
            # 검색 결과 내 로그 정규화
            # -------------------------
            google_df["리뷰 수"] = pd.to_numeric(
                google_df["리뷰 수"],
                errors="coerce"
            ).fillna(0)

            max_reviews = (
                google_df["리뷰 수"].max()
            )

            if max_reviews > 0:

                google_df["리뷰기준점수"] = (
                    np.log1p(
                        google_df["리뷰 수"]
                    )
                    /
                    np.log1p(max_reviews)
                    * 100
                )

            else:

                google_df["리뷰기준점수"] = 0


            # -------------------------
            # 업력 기준점수
            # -------------------------
            google_df["업력기준점수"] = (
                google_df["업력"]
                .apply(calc_age_score)
            )


            # -------------------------
            # 종합점수
            # -------------------------
            google_df["종합점수"] = (

                google_df["평점기준점수"]
                * rating_weight / 100

                +

                google_df["리뷰기준점수"]
                * review_weight / 100

                +

                google_df["업력기준점수"]
                * age_weight / 100

            ).round(1)


            # -------------------------
            # 60점 이상 / 최대 20개
            # -------------------------
            result_df = (
                google_df[
                    google_df["종합점수"] >= 60
                ]
                .sort_values(
                    "종합점수",
                    ascending=False
                )
                .head(20)
                .reset_index(drop=True)
            )

            result_df["순위"] = (
                result_df.index + 1
            )


            # -------------------------
            # 업력 화면 표시
            # -------------------------
            result_df["업력 표시"] = (
                result_df["업력"]
                .apply(
                    lambda x:
                    f"{x:.1f}년"
                    if pd.notna(x)
                    else "정보 없음"
                )
            )


            # -------------------------
            # Google Maps URL
            # -------------------------
            result_df["지도"] = (
                result_df.apply(
                    lambda row:
                    "https://www.google.com/maps/search/?api=1"
                    "&query="
                    + urllib.parse.quote(
                        row["식당명"]
                        + " "
                        + row["주소"]
                    )
                    + "&query_place_id="
                    + row["Place ID"],
                    axis=1
                )
            )


            # -------------------------
            # 소비자용 출력
            # -------------------------
            consumer_df = result_df[
                [
                    "순위",
                    "식당명",
                    "Google 평점",
                    "리뷰 수",
                    "업력 표시",
                    "종합점수",
                    "주소",
                    "지도"
                ]
            ].copy()

            consumer_df.columns = [
                "순위",
                "식당명",
                "Google 평점",
                "리뷰 수",
                "업력",
                "종합점수",
                "주소",
                "지도"
            ]

            st.success(
                f"{food} 추천 맛집 "
                f"{len(consumer_df)}곳입니다."
            )

            # =====================================
            # 소비자용 카드형 결과 + 대표사진
            # =====================================

            st.subheader("🏆 추천 맛집 순위")

            for _, row in result_df.iterrows():

                rank = int(row["순위"])
                name = str(row["식당명"])
                rating = row["Google 평점"]
                reviews = int(row["리뷰 수"])

                age = (
                    f"{row['업력']:.1f}년"
                    if pd.notna(row["업력"])
                    else "정보 없음"
                )

                score = float(row["종합점수"])
                address = str(row["주소"])
                map_url = str(row["지도"])
                photo_url = str(row.get("대표사진", ""))

                if rank == 1:
                    rank_icon = "🥇"
                elif rank == 2:
                    rank_icon = "🥈"
                elif rank == 3:
                    rank_icon = "🥉"
                else:
                    rank_icon = f"{rank}위"

                with st.container(border=True):

                    image_col, info_col = st.columns(
                        [1.4, 3.6]
                    )

                    with image_col:

                        if photo_url:
                            st.image(
                                photo_url,
                                use_container_width=True
                            )
                        else:
                            st.markdown(
                                "📷 대표사진 없음"
                            )

                    with info_col:

                        st.markdown(
                            f"### {rank_icon} [{name}]({map_url})"
                        )

                        metric1, metric2, metric3 = st.columns(3)

                        with metric1:
                            st.metric(
                                "Google 평점",
                                f"{rating:.1f}"
                            )

                        with metric2:
                            st.metric(
                                "리뷰",
                                f"{reviews:,}개"
                            )

                        with metric3:
                            st.metric(
                                "종합점수",
                                f"{score:.1f}"
                            )

                        st.markdown(
                            f"🕰 **업력:** {age}"
                        )

                        st.caption(address)

                        st.link_button(
                            "📍 Google 지도에서 보기",
                            map_url,
                            use_container_width=True
                        )

            st.caption(
                "업력은 행정 인허가일 기준입니다. "
                "업력 정보가 없는 식당은 내부 계산에서 "
                "중립점수 50점을 적용합니다."
            )
