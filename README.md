# Weigou Manager (微购 상품관리)

Windows 데스크톱 상품 수집·등록·이미지 검색 도구입니다.

## 다른 PC에서 사용

1. [Releases](https://github.com/lee3215-ko/weigou-manager/releases/latest) 에서 `WeigouManager.zip` 다운로드
2. 압축 해제 후 `WeigouManager.exe` 또는 `실행.bat` 실행
3. 프로그램을 **껐다 켜면** 새 버전이 있으면 자동 업데이트됩니다

## 목록 실시간 공유 (여러 이용자)

상품 / 제외 / 등록 목록을 **Supabase**로 실시간 동기화합니다.  
(홈페이지 등록과 같은 `data/mall_cloud.json` 사용 · GitHub 토큰 불필요)

1. 각 PC에 `data/mall_cloud.json` 이 있어야 합니다 (supabaseUrl / serviceRoleKey)
2. 프로그램 **[목록 동기화]** → 이 PC 이름 입력 → 저장 후 동기화
3. 내 변경은 저장 직후 바로 올리고, 다른 PC 변경은 약 **2초**마다 받아옵니다

설정 파일: `data/sync_settings.json`  
(예제: `data/sync_settings.example.json`)

이미지 URL이 있으면 각 PC에서 자동 다운로드합니다.

이후 기능 배포: 이 폴더에서 `deploy.bat` (또는 `.\scripts\publish.ps1 -Notes "..."`)  
→ 이용자가 프로그램을 **껐다 켜면** 자동 업데이트됩니다.

## 개발 / 배포

```bat
deploy.bat
```

또는:

```powershell
.\scripts\publish.ps1 -Notes "변경 요약"
```

- 버전: `paths.py` 의 `APP_VERSION`
- 업데이트 메타: `version.json` (publish 시 자동)

## 로컬 실행

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python run.py
```
