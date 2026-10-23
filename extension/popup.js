document.getElementById('clipBtn').addEventListener('click', () => {
  const statusEl = document.getElementById('status');
  statusEl.textContent = '본문 영역 추출 및 개인정보 필터 적용 중...';
  chrome.runtime.sendMessage({ type: 'KG_CLIP_ACTIVE_TAB' }, (res) => {
    if (res && res.ok) {
      statusEl.textContent = `수집함 저장 완료 (큐 ${res.queued}건 / 진행 오프셋 ${res.cursor})`;
    } else {
      statusEl.textContent = '담기 실패: ' + ((res && res.error) || '알 수 없는 오류');
    }
  });
});
