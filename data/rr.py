"""Minimal Revised Romanization of Korean (syllable-based, basic consonant-assimilation rules)."""
I=['g','kk','n','d','tt','r','m','b','pp','s','ss','','j','jj','ch','k','t','p','h']
V=['a','ae','ya','yae','eo','e','yeo','ye','o','wa','wae','oe','yo','u','wo','we','wi','yu','eu','ui','i']
F=['','k','k','k','n','n','n','t','l','k','m','l','l','l','p','l','m','p','p','t','t','ng','t','t','k','t','p','t']
def dec(c):
    o=ord(c)-0xAC00
    return (o//588,(o%588)//28,o%28) if 0<=o<11172 else None
def rr(s):
    out=[];ch=list(s)
    for idx,c in enumerate(ch):
        x=dec(c)
        if not x: out.append(c);continue
        i,v,f=x; ini=I[i]
        prev=dec(ch[idx-1]) if idx>0 else None
        if idx==0 or not prev: ini='r' if False else ini
        if i==5 and (idx==0 or not prev): ini='r'   # initial ㄹ
        if prev and i==5 and prev[2]==8: ini='l'   # ㄹㄹ -> ll
        if prev and i==11 and prev[2] in (1,): pass
        nx=dec(ch[idx+1]) if idx+1<len(ch) else None
        fin=F[f]
        if nx and nx[0]==11 and f: # liaison before ㅇ
            fin={'k':'g','t':'d','p':'b','l':'r','n':'n','m':'m','ng':'ng'}.get(fin,fin)
            if f==1: fin='g'
        out.append(ini+V[v]+fin)
    return ''.join(out)
SURNAME={'김':'Kim','이':'Lee','박':'Park','최':'Choi','정':'Jung','강':'Kang','조':'Cho','윤':'Yoon','장':'Jang','임':'Lim','한':'Han','오':'Oh','서':'Seo','신':'Shin','권':'Kwon','황':'Hwang','안':'Ahn','송':'Song','문':'Moon','노':'Noh','배':'Bae','전':'Jeon','성':'Sung','엄':'Eom','손':'Son'}
def person(n):
    n=n.strip(); sur=n[0]; given=n[1:]
    g='-'.join(rr(c) for c in given) if len(given)==2 else rr(given)
    return f"{SURNAME.get(sur,rr(sur).capitalize())} {g.capitalize()}"
def place(n): return ' '.join(rr(w).capitalize() for w in n.split())
