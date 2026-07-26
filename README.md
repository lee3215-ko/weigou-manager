# Weigou Manager (微购 상품관리)

Windows 데스크톱 상품 수집·등록·이미지 검색 도구입니다.

## 다른 PC에서 사용

1. [Releases](https://github.com/lee3215-ko/weigou-manager/releases/latest) 에서 `WeigouManager.zip` 다운로드
2. 압축 해제 후 `WeigouManager.exe` 또는 `실행.bat` 실행
3. 프로그램을 **껐다 켜면** 새 버전이 있으면 자동 업데이트됩니다

## 목록 실시간 공유 (여러 이용자)

상품 / 제외 / 등록 목록을 GitHub로 동기화합니다.

1. GitHub → Settings → Developer settings → Personal access token  
   (`repo` 권한, private 저장소면 Contents 읽기·쓰기)
2. 프로그램에서 **[목록 동기화]** → 토큰 입력 → 저장
3. 모든 PC에 **같은 토큰**(또는 각자 토큰)을 넣고 켜 두면 약 12초마다 서로 반영됩니다

설정 파일: `data/sync_settings.json`  
(예제: `data/sync_settings.example.json`)

이미지 파일은 URL이 있으면 각 PC에서 자동으로 내려받습니다.

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
