import re
from robobrowser import RoboBrowser
b = RoboBrowser()
b.addheaders = [('User-agent', 'Mozilla/5.0 (X11; U; Linux i686; en-US; rv:1.9.0.1) Gecko/2008071615 Fedora/3.0.1-1.fc9 Firefox/3.0.1')]

listid = "list-ig.txt"
#FORMAT 1 (URL) : https://www.instagram.com/aldisaw/
#FORMAT 2 username : aldisaw

try:
    listid = open(listid, "r")
except:
    print ("\nList Not Found!")
    quit()

for username in listid:
    if username.strip() == "":
        print("BARIS KOSONG")
    else:
        #b.open("http://www.otzberg.net/iguserid/index.php")
        #form = b.get_form()
        #form['username'] = username.strip().replace("https://www.instagram.com/","").replace("/","")
        #b.method = "POST"
        #b.submit_form(form)
        #soup = b.parsed
        #tag = soup.find_all('div')
        
        #start = '<b>User-ID: </b>'
        #end = '<br/>'
        
        #def convert(s):
        #    str1 = ""
        #    return(str1.join(s))
        #if b.response.status_code==200:
        #    div = str(soup.find(class_='hero-unit'))
        #    s = re.findall(r'\d+', div)
        #    result = re.search('%s(.+?)%s' % (start, end), div).group(1)
        #    print(result)
        #else:
        #    print("KONEKSI BURUK / WEBSITE TIDAK DAPAT DIAKSES")
            
        b.open("https://thumbtube.com/instagram-user-id-finder#userid")
        form = b.get_form()
        form['uname'] = username.strip().replace("https://www.instagram.com/","").replace("/","")
        b.method = "POST"
        b.submit_form(form)
        soup = b.parsed
        tag = soup.find_all('input')
        def convert(s):
            str1 = ""
            return(str1.join(s))
        if b.response.status_code==200:
            div = str(soup.find(id='yttg_url_copy'))
            s = re.findall(r'\d+', div)
            print(convert(s))
        else:
            print("KONEKSI BURUK / WEBSITE TIDAK DAPAT DIAKSES")
    
print("============= D O N E ==============")


