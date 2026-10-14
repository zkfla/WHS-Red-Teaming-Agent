# WHS Red Teaming Agent 기술 문서 사이트

이 `docs-site` 브랜치는 사이트 원본과 배포 설정만 관리한다.
애플리케이션 개발은 `dev`에서 진행하며, 두 브랜치를 서로 병합하지 않는다.
사이트 수정은 `docs-site`에만 반영한다.

## 구조

- `website/docs/`: 페이지 Markdown과 이미지
- `website/mkdocs.yml`: 메뉴와 테마 설정
- `website/requirements.txt`: 문서 빌드 의존성
- `.github/workflows/docs.yml`: 사이트 빌드와 GitHub Pages 직접 배포
- `website/site/`: 로컬에서 생성하는 HTML. Git에 커밋하지 않는다.

`docs-site`에 사이트나 워크플로 변경을 push하면 엄격 모드로 빌드하고,
성공한 결과만 Pages에 직접 배포한다. HTML을 별도 브랜치에 커밋하지 않는다.
`docs-site`를 대상으로 한 PR에서는 빌드만 검증하고 게시하지 않는다.

## GitHub 최초 전환 설정

1. **Settings → Pages → Build and deployment → Source**를
   **GitHub Actions**로 바꾼다.
2. **Settings → Environments → github-pages**의 배포 브랜치 정책에서
   `docs-site`의 배포를 허용한다. 기존 승인자 등 다른 보호 규칙은 유지한다.
3. `docs-site`를 push하고 Actions의 **Deploy docs site**가 성공했는지 확인한다.
   설정 전에 실패한 실행은 설정 완료 후 **Re-run jobs**로 다시 실행할 수 있다.
4. 사이트가 정상 표시되는 것을 확인한 뒤 기존 원격 `gh-pages` 브랜치를 정리한다.

기본 `GITHUB_TOKEN`의 Pages 배포 권한을 사용하므로 이전 `GH_PAGES_TOKEN`
Secret은 이 워크플로에서 사용하지 않는다. 저장소 공개 여부는 자동으로 변경하지
않으며, Pages를 사용할 수 있는 저장소 공개 범위와 요금제가 필요하다.
`dev`에는 사이트 워크플로를 두지 않으므로 수동 실행 버튼에
의존하지 않는다. push 또는 기존 실행의 재실행으로 배포한다.

## 문서 수정

기존 작업을 저장한 뒤 사이트 브랜치로 이동한다.

```sh
git switch docs-site
```

`website/docs/`의 문서·이미지와 `website/mkdocs.yml`의 메뉴를 수정한다.
필요하면 `docs-site`에서 작업 브랜치를 만들고 PR 대상도 `docs-site`로 지정한다.
사이트 변경을 `dev`에 병합하지 않는다.

저장소 루트에서 문서용 환경을 준비하고 빌드를 확인한다.

```sh
python3 -m venv website/.venv-docs
. website/.venv-docs/bin/activate
python -m pip install -r website/requirements.txt
python -m mkdocs build --strict --config-file website/mkdocs.yml
```

브라우저로 미리 보려면 다음을 실행한다.

```sh
python -m mkdocs serve --config-file website/mkdocs.yml
```

변경한 소스만 커밋한 뒤 `git push origin docs-site`로 게시한다.
빌드 또는 배포가 실패하면 Actions 로그를 확인한다. 성공 전에는 사이트가
갱신되었다고 판단하지 않는다.

참고: [GitHub Pages 사용자 지정 워크플로](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).
