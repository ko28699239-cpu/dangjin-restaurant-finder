    
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
# ---------------------------------------------------------
# UI 대분류 -> 행정데이터 업태구분명 매핑
# ---------------------------------------------------------

CATEGORY_ADMIN_TYPES = {
    "🍚 한식": ["한식"],

    "🥟 중국식": ["중국식"],

    "🍣 일식": ["일식"],

    "🍝 경양식": ["경양식"],

    "🌶️ 분식": ["분식"],

    "🐟 횟집": ["횟집"],

    "🥩 식육(숯불구이)": [
        "식육(숯불구이)"
    ],

    # 화면에서는 하나의 카테고리지만
    # 행정데이터에서는 두 업태를 모두 포함
    "🍗 호프/통닭": [
        "호프/통닭",
        "통닭(치킨)"
    ]
}


def filter_admin_by_category(admin_df, selected_category):
    """
    사용자가 선택한 대분류에 맞는
    행정 인허가 음식점만 반환
    """

    allowed_types = CATEGORY_ADMIN_TYPES.get(
        selected_category,
        []
    )

    # 매핑되지 않은 카테고리라면
    # 전체 데이터를 그대로 사용
    if not allowed_types:
        return admin_df.copy()

    if "업태구분명" not in admin_df.columns:
        return admin_df.copy()

    category_series = (
        admin_df["업태구분명"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    filtered = admin_df[
        category_series.isin(allowed_types)
    ].copy()

    return filtered

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


# ---------------------------------------------------------
# Google Places 검색
# 같은 검색조건은 7일 캐시
# 대분류 업태를 Google Place Type으로 먼저 제한한 뒤
# 그 안에서 음식명을 검색
# ---------------------------------------------------------

@st.cache_data(ttl=60 * 60 * 24 * 7)
def search_google_places(food, category):

    url = "https://places.googleapis.com/v1/places:searchText"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": ",".join([
            "places.id",
            "places.displayName",
            "places.formattedAddress",
            "places.rating",
            "places.userRatingCount",
            "places.photos",
            "places.types",
            "places.primaryType",
            "places.primaryTypeDisplayName"
        ])
    }

    # -----------------------------------------------------
    # 우리 행정 업태 → Google Places 식당 타입
    # -----------------------------------------------------
    category_type_map = {

        "🍚 한식": "korean_restaurant",

        "🥟 중국식": "chinese_restaurant",

        "🍣 일식": "japanese_restaurant",

        "🍝 경양식": "western_restaurant",

        # 분식은 정확한 국가형 업종이 없으므로
        # snack_bar를 이용
        "🌶️ 분식": "snack_bar",

        "🐟 횟집": "seafood_restaurant",

        "🥩 식육(숯불구이)": "korean_barbecue_restaurant",

        "🍗 호프/통닭": "chicken_restaurant"
    }

    google_type = category_type_map.get(
        category,
        "restaurant"
    )

    # 화면용 이모지 제거
    category_text = (
        category
        .replace("🍚", "")
        .replace("🥟", "")
        .replace("🍣", "")
        .replace("🍝", "")
        .replace("🌶️", "")
        .replace("🐟", "")
        .replace("🥩", "")
        .replace("🍗", "")
        .strip()
    )

    # -----------------------------------------------------
    # 검색어
    #
    # 예:
    # 중국식 + 볶음밥
    # → "충남 당진시 중국식 볶음밥"
    #
    # 중요한 점:
    # 음식명만 검색하지 않고 대분류를 끝까지 유지
    # -----------------------------------------------------

    if food and food != category:

        text_query = (
            f"충남 당진시 {category_text} {food}"
        )

    else:

        text_query = (
            f"충남 당진시 {category_text} 음식점"
        )

    data = {
        "textQuery": text_query,
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

        st.error(
            f"Google API 오류: "
            f"{response.status_code}"
        )

        st.code(response.text)
        st.stop()

    places = response.json().get(
        "places",
        []
    )

    rows = []

    for place in places:

        photos = place.get(
            "photos",
            []
        )

        photo_url = ""

        if photos:

            photo_name = photos[0].get(
                "name",
                ""
            )

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
                place.get(
                    "displayName",
                    {}
                ).get(
                    "text",
                    ""
                ),

            "Google 평점":
                place.get(
                    "rating",
                    np.nan
                ),

            "리뷰 수":
                place.get(
                    "userRatingCount",
                    0
                ),

            "주소":
                place.get(
                    "formattedAddress",
                    ""
                ),

            "Place ID":
                place.get(
                    "id",
                    ""
                ),

            "대표사진":
                photo_url,

            # 디버깅/검증용
            "Google 업종":
                place.get(
                    "primaryType",
                    ""
                ),

            "Google 업종명":
                place.get(
                    "primaryTypeDisplayName",
                    {}
                ).get(
                    "text",
                    ""
                )
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
# [V1-1] 계층형 음식 선택 UX
# 1단계: 업태 대분류
# 2단계: 중분류
# 3단계: 필요할 때만 세부 음식
# 어느 단계에서도 바로 검색 가능
# =========================================================

st.subheader("🍽️ 음식 선택")


# ---------------------------------------------------------
# 업태 대분류 → 중분류 → 세부 음식
# ---------------------------------------------------------
food_tree = {

    "🍚 한식": {
        "백반·한정식": [
            "백반",
            "한정식",
            "쌈밥",
            "비빔밥"
        ],

        "국밥·해장국": [
            "순대국",
            "돼지국밥",
            "해장국",
            "육개장"
        ],

        "탕·곰탕": [
            "갈비탕",
            "설렁탕",
            "곰탕",
            "삼계탕",
            "감자탕"
        ],

        "찌개·전골": [
            "김치찌개",
            "된장찌개",
            "청국장",
            "부대찌개",
            "전골"
        ],

        "고기요리": [
            "불고기",
            "제육볶음",
            "두루치기"
        ],

        "닭·오리": [
            "닭볶음탕",
            "백숙",
            "오리백숙",
            "오리주물럭"
        ],

        "면·국수": [
            "칼국수",
            "콩국수",
            "잔치국수",
            "비빔국수",
            "막국수"
        ],

        "족발·보쌈": [
            "족발",
            "보쌈"
        ],

        "생선·해산물": [
            "생선구이",
            "해물탕",
            "아귀찜",
            "매운탕"
        ],

        "향토음식": [
            "어죽",
            "추어탕",
            "게국지"
        ]
    },


    "🥟 중국식": {
        "짜장면": [],
        "짬뽕": [],
        "볶음밥": [],
        "탕수육": [],
        "마라탕": [],
        "마라샹궈": [],
        "양꼬치": [],
        "딤섬·만두": [],
        "깐풍기": [],
        "중식코스": []
    },


    "🍣 일식": {
        "초밥": [],
        "사시미·회": [],
        "돈카츠": [],
        "라멘": [],
        "우동": [],
        "소바": [],
        "덮밥": [],
        "일식카레": [],
        "이자카야": [],
        "일식코스": []
    },


    "🍝 경양식": {
        "파스타": [],
        "피자": [],
        "스테이크": [],
        "리조또": [],
        "볶음밥": [],
        "오므라이스": [],
        "돈가스": [],
        "함박스테이크": [],
        "샐러드": [],
        "양식코스": []
    },


    "🌶️ 분식": {
        "떡볶이": [],
        "김밥": [],
        "순대": [],
        "튀김": [],
        "라볶이": [],
        "만두": [],
        "어묵": [],
        "쫄면": [],
        "떡꼬치": [],
        "닭강정": []
    },


    "🐟 횟집": {
        "모둠회": [],
        "광어회": [],
        "우럭회": [],
        "도미회": [],
        "물회": [],
        "회덮밥": [],
        "해산물": [],
        "매운탕": [],
        "조개": [],
        "제철회": []
    },


    "🥩 식육(숯불구이)": {
        "삼겹살": [],
        "돼지갈비": [],
        "한우": [],
        "소갈비": [],
        "갈비살": [],
        "등심": [],
        "목살": [],
        "항정살": [],
        "곱창·막창": [],
        "숯불구이": []
    },


    "🍗 호프/통닭": {
        "후라이드치킨": [],
        "양념치킨": [],
        "간장치킨": [],
        "닭강정": [],
        "닭발": [],
        "골뱅이": [],
        "마른안주": [],
        "먹태": [],
        "생맥주안주": [],
        "치킨·호프": []
    }
}


# ---------------------------------------------------------
# 세션 상태 초기화
# ---------------------------------------------------------
default_category = list(food_tree.keys())[0]

if (
    "selected_category" not in st.session_state
    or st.session_state.selected_category not in food_tree
):
    st.session_state.selected_category = default_category

if "selected_group" not in st.session_state:
    st.session_state.selected_group = ""

if "selected_food" not in st.session_state:
    st.session_state.selected_food = ""

if "custom_food" not in st.session_state:
    st.session_state.custom_food = ""


# ---------------------------------------------------------
# 1단계 - 업태 대분류
# ---------------------------------------------------------
st.markdown("### 1. 음식점 종류")

category_cols = st.columns(4)

for i, category in enumerate(food_tree.keys()):

    selected = (
        st.session_state.selected_category == category
    )

    with category_cols[i % 4]:

        if st.button(
            category,
            key=f"category_{category}",
            type="primary" if selected else "secondary",
            use_container_width=True
        ):
            st.session_state.selected_category = category

            # 아래 단계 초기화
            st.session_state.selected_group = ""
            st.session_state.selected_food = ""
            st.session_state.custom_food = ""

            st.rerun()


# ---------------------------------------------------------
# 2단계 - 중분류
# ---------------------------------------------------------
current_category = st.session_state.selected_category
group_names = list(food_tree[current_category].keys())

st.markdown("### 2. 세부 종류")

group_cols = st.columns(5)

for i, group_name in enumerate(group_names):

    selected = (
        st.session_state.selected_group == group_name
    )

    with group_cols[i % 5]:

        if st.button(
            group_name,
            key=f"group_{current_category}_{group_name}",
            type="primary" if selected else "secondary",
            use_container_width=True
        ):
            st.session_state.selected_group = group_name
            st.session_state.selected_food = ""
            st.session_state.custom_food = ""

            st.rerun()


# ---------------------------------------------------------
# 3단계 - 세부 음식
# 한식처럼 3단계가 필요한 경우만 표시
# ---------------------------------------------------------
if st.session_state.selected_group:

    detail_foods = food_tree[current_category][
        st.session_state.selected_group
    ]

    if detail_foods:

        st.markdown("### 3. 음식 선택")

        detail_cols = st.columns(5)

        for i, food_name in enumerate(detail_foods):

            selected = (
                st.session_state.selected_food == food_name
            )

            with detail_cols[i % 5]:

                if st.button(
                    food_name,
                    key=f"detail_{current_category}_{food_name}",
                    type="primary" if selected else "secondary",
                    use_container_width=True
                ):
                    st.session_state.selected_food = food_name
                    st.session_state.custom_food = ""

                    st.rerun()


# ---------------------------------------------------------
# 직접 검색
# ---------------------------------------------------------
st.markdown("### 🔎 직접 음식 검색")

custom_food = st.text_input(
    "직접 검색",
    placeholder="예: 샤브샤브, 민물새우탕, 쭈꾸미",
    key="custom_food",
    label_visibility="collapsed"
)


# ---------------------------------------------------------
# 최종 검색 조건 결정
# 3단계 > 2단계 > 1단계 순
# ---------------------------------------------------------

if st.session_state.custom_food.strip():

    search_level = "custom"
    food = st.session_state.custom_food.strip()

elif st.session_state.selected_food:

    search_level = "detail"
    food = st.session_state.selected_food

elif st.session_state.selected_group:

    search_level = "group"
    food = st.session_state.selected_group

else:

    search_level = "category"
    food = st.session_state.selected_category


# 검색에 사용할 행정 업태명
selected_category = st.session_state.selected_category


# 화면 표시
st.caption(
    f"현재 검색 조건: **{selected_category}"
    + (
        f" → {st.session_state.selected_group}"
        if st.session_state.selected_group
        else ""
    )
    + (
        f" → {st.session_state.selected_food}"
        if st.session_state.selected_food
        else ""
    )
    + "**"
)

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

# ---------------------------------------------------------
# 맛집 검색 실행
# ---------------------------------------------------------

if st.button(
    "🔎 맛집 찾기",
    type="primary",
    use_container_width=True
):

    # -----------------------------------------------------
    # 입력값 / 가중치 확인
    # -----------------------------------------------------
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

            # -------------------------------------------------
            # 1. Google Places 검색
            # 음식명 + 선택 대분류를 함께 전달
            # -------------------------------------------------
            google_df = search_google_places(
                food.strip(),
                selected_category
            )

            if google_df.empty:

                st.warning(
                    "검색된 식당이 없습니다."
                )

                st.stop()


            # -------------------------------------------------
            # 2. 당진 행정데이터 로드
            # -------------------------------------------------
            admin_df = load_admin_data()


            # -------------------------------------------------
            # 3. 선택한 업태구분명으로 행정데이터 제한
            #
            # 예:
            # 중국식 -> 중국식
            # 일식 -> 일식
            # 식육(숯불구이) -> 식육(숯불구이)
            # 호프/통닭 -> 호프/통닭 + 통닭(치킨)
            # -------------------------------------------------
            admin_df = filter_admin_by_category(
                admin_df,
                selected_category
            )


            # -------------------------------------------------
            # 4. Google 결과 중
            #    선택 업태의 행정데이터와 실제 매칭되는
            #    식당만 유지
            # -------------------------------------------------
            matched_rows = []

            for _, row in google_df.iterrows():

                age_info = find_business_age(
                    row["식당명"],
                    row["주소"],
                    admin_df
                )

                if age_info:

                    matched_row = row.copy()

                    matched_row["업력"] = (
                        age_info["업력"]
                    )

                    matched_rows.append(
                        matched_row
                    )


            google_df = pd.DataFrame(
                matched_rows
            )


            # -------------------------------------------------
            # 5. 행정 업태와 매칭된 식당이 없는 경우
            # -------------------------------------------------
            if google_df.empty:

                st.warning(
                    "선택한 음식점 종류와 "
                    "일치하는 식당을 찾지 못했습니다."
                )

                st.stop()


            # -------------------------------------------------
            # 6. 인덱스 정리
            # -------------------------------------------------
            google_df = google_df.reset_index(
                drop=True
            )


            # -------------------------------------------------
            # 7. 평점 기준점수
            # Google 평점 5점 = 100점
            # -------------------------------------------------
            google_df["평점기준점수"] = (
                pd.to_numeric(
                    google_df["Google 평점"],
                    errors="coerce"
                )
                / 5
                * 100
            )


            # -------------------------------------------------
            # 8. 리뷰 기준점수
            # 검색된 식당 내 로그 정규화
            # -------------------------------------------------
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
                    / np.log1p(max_reviews)
                    * 100
                )

            else:

                google_df["리뷰기준점수"] = 0


            # -------------------------------------------------
            # 9. 업력 기준점수
            # -------------------------------------------------
            google_df["업력기준점수"] = (
                google_df["업력"]
                .apply(calc_age_score)
            )


            # -------------------------------------------------
            # 10. 종합점수
            # -------------------------------------------------
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
        
        
