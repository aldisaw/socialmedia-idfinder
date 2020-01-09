import re
from robobrowser import RoboBrowser
b = RoboBrowser();
b.addheaders = [('User-agent', 'Mozilla/5.0 (X11; U; Linux i686; en-US; rv:1.9.0.1) Gecko/2008071615 Fedora/3.0.1-1.fc9 Firefox/3.0.1')]

listid = "list-id.txt"
#FORMAT(URL) : https://twitter.com/aldisaw/
#FORMAT(URL) : https://www.instagram.com/aldisaw/
#FORMAT(URL) : https://www.facebook.com/aldi.katadji/

try:
    listid = open(listid, "r")
except:
    print ("\nList Not Found!")
    quit()

for username in listid:
    if username.strip() == "":
        print("BARIS KOSONG")
    elif re.search("twitter",username.strip()):
        b.open("http://findmyfbid.in/find-my-twitter-id/")
        form = b.get_form()
        form['handle'] = username.strip().replace("https://twitter.com/","").replace("/","")
        b.method = "POST"
        b.submit_form(form)
        
        src = str(b.parsed())
        
        start = '<code>'
        end = '</code>'
        if b.response.status_code==200:
            result = re.search('%s(.+?)%s' % (start, end), src).group(1)
            print(result)
        else:
            print("KONEKSI BURUK / WEBSITE TIDAK DAPAT DIAKSES")
    elif re.search("instagram",username.strip()):
        b.open("http://www.otzberg.net/iguserid/index.php")
        form = b.get_form()
        form['username'] = username.strip().replace("https://www.instagram.com/","").replace("/","")
        b.method = "POST"
        b.submit_form(form)
        soup = b.parsed
        tag = soup.find_all('div')
        
        start = '<b>User-ID: </b>'
        end = '<br/>'
        
        def convert(s):
            str1 = ""
            return(str1.join(s))
        if b.response.status_code==200:
            div = str(soup.find(class_='hero-unit'))
            s = re.findall(r'\d+', div)
            result = re.search('%s(.+?)%s' % (start, end), div).group(1)
            print(result)
        else:
            print("KONEKSI BURUK / WEBSITE TIDAK DAPAT DIAKSES")
    elif re.search("facebook",username.strip()):
        b.open("https://findmyfbid.com/")
        form = b.get_form()
        form['url'] = username.strip()
        b.method = "POST"
        b.submit_form(form)
        soup = str(b.parsed)
        
        def convert(s):
            str1 = ""
            return(str1.join(s))
        
        if b.response.status_code==200:
            s = re.findall(r'\d+', soup)
            print(convert(s))
        else:
            print("KONEKSI BURUK / WEBSITE TIDAK DAPAT DIAKSES")
print("============= D O N E ==============")