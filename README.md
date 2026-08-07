# Weigou Manager (微购 상품관리)

Windows 데스크톱 상품 수집·등록·이미지 검색 도구입니다.

## 다른 PC에서 사용

1. [Releases](https://github.com/lee3215-ko/weigou-manager/releases/latest) 에서 `WeigouManager.zip` 다운로드
2. 압축 해제 후 `WeigouManager.exe` 또는 `실행.bat` 실행
3. 프로그램을 **껐다 켜면** 새 버전이 있으면 자동 업데이트됩니다

## 목록 자동 공유 (여러 이용자)

상품 / 제외 / 등록 목록은 **클라우드(Supabase)가 본체**입니다.  
프로그램을 켜면 자동으로 맞춰지며, 수동 「동기화」 버튼은 필요 없습니다.

- 하단 상태: `앨범:`(微购 연결) 과 `목록:`(클라우드 동기화) 이 따로 표시됩니다
- 배포 zip에 `bundled/mall_cloud.json` + `data/mall_cloud.json` 이 포함됩니다
- 업데이트 시 `data/` 가 보존되어도 `bundled/` 로 설정을 복구합니다
- 내 변경은 저장 직후 업로드, 다른 PC 변경은 약 **2초**마다 반영

고급 설정만 **[동기화 설정]** 에서 변경 (PC 이름·역할 등)

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
